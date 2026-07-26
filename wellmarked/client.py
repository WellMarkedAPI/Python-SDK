"""Synchronous WellMarked client."""
from __future__ import annotations

import time
from typing import Iterable, Optional, Union, Any

import httpx

from ._base import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    backoff_seconds,
    is_safe_to_replay,
    merge_headers,
    new_idempotency_key,
    parse_response,
    policy_overrides,
    resolve_api_key,
    sanitize_headers,
    wrap_transport_error,
)
from .models import (
    ApiKeyInfo, BulkJob, CrawlJob, CreatedKey, ExtractResult, LogsPage,
    RegisteredAccount, RevokedKey, RotatedKey, RotatedWebhookSecret,
    SearchResults, Usage,
)


class WellMarked:
    """Synchronous client for the WellMarked API.

    The client is a thin, typed wrapper around the HTTP API. It can be used
    directly or as a context manager (recommended — guarantees the underlying
    connection pool is released)::

        from wellmarked import WellMarked

        with WellMarked(api_key="wm_...") as wm:
            result = wm.extract("https://example.com/article")
            print(result.markdown)

    The API key can also be passed via the ``WELLMARKED_API_KEY`` environment
    variable, in which case ``WellMarked()`` is enough.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Create a sync client.

        Args:
            api_key: Your WellMarked API key (``wm_...``). Falls back to the
                ``WELLMARKED_API_KEY`` environment variable.
            timeout: Timeout for a single attempt, seconds. This is **per
                attempt, not per call**: with the default ``max_retries=2`` a
                retryable request can take up to roughly ``timeout * 3`` plus
                backoff (~92s at the defaults) before giving up. Lower
                ``max_retries`` if you need a tighter ceiling.
            max_retries: How many times to retry a request the SDK knows is
                safe to replay. Defaults to 2 (3 attempts total); 0 disables.
                Only connection failures and 5xx are retried, and only for
                GETs and POSTs carrying an ``Idempotency-Key`` (which
                :meth:`bulk` and :meth:`crawl` send automatically). A
                ``POST /extract`` is never retried — the API bills it on
                arrival, and a connection error can't tell you whether it
                arrived. Retries reuse the same key, so the API replays the
                original job rather than enqueuing a second one.
            headers: Extra headers sent on every request — useful for adding
                an internal correlation id, a custom user agent suffix, etc.
                Authorization / Content-Type / Accept are reserved and silently
                ignored if passed (the SDK manages those itself).
        """
        self._api_key = resolve_api_key(api_key)
        self._base_url = DEFAULT_BASE_URL
        self._max_retries = max(0, max_retries)
        self._extra_headers: dict[str, str] = dict(headers or {})
        merged = merge_headers(self._api_key, self._extra_headers)
        self._client: httpx.Client = httpx.Client(
            base_url=self._base_url,
            headers=merged,
            timeout=timeout,
        )

    # ── Self-registration ─────────────────────────────────────────────────────

    @classmethod
    def register(
        cls,
        email: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> RegisteredAccount:
        """Self-register for a free, extract-only API key — no existing key needed.

        The zero-to-first-call path for an agent that discovered WellMarked
        programmatically. Returns a :class:`RegisteredAccount` whose ``api_key``
        is a weak credential (Free plan, ``scopes=["extract"]``); build a client
        with it::

            account = WellMarked.register("agent@example.com")
            with WellMarked(api_key=account.api_key) as wm:
                wm.extract("https://example.com")

        Not retried — registration mints an account and isn't idempotent.

        Raises:
            RateLimitError: ``register_rate_limited`` — too many registrations
                from this IP.
            APIStatusError: ``email_taken`` (409) if the email already has an
                account; ``service_unavailable`` (503) if the limiter backend is
                momentarily down (retry shortly).
        """
        url = f"{DEFAULT_BASE_URL}/register"
        client = httpx.Client(timeout=timeout)
        try:
            try:
                response = client.post(url, json={"email": email})
            except httpx.HTTPError as exc:
                raise wrap_transport_error(exc)
            try:
                assert response.content
                body = response.json()
            except (AssertionError, ValueError):
                body = None
            data = parse_response(
                response.status_code, body, headers=dict(response.headers),
            )
        finally:
            client.close()
        return RegisteredAccount.from_response(data)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "WellMarked":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def extract(
        self,
        url: str,
        *,
        render_js: bool = False,
        format: str = "markdown",
        retry: int = 0,
        allow_domains: Optional[Iterable[str]] = None,
        deny_patterns: Optional[Iterable[str]] = None,
        respect_robots: Optional[str] = None,
    ) -> ExtractResult:
        """Extract clean Markdown from a single URL.

        Args:
            url: The URL to extract content from.
            render_js: Use Playwright to render JS-heavy pages. Requires a
                Pro, Growth, or Enterprise plan; Free returns
                ``plan_not_supported``.
            format: Output format — ``"markdown"`` (default), ``"json"``
                (typed heading/paragraph/list/code blocks), ``"chunks"``
                (contiguous 500-token windows for embedding), ``"html"`` (the
                raw fetched HTML) or ``"links"`` (every http(s) link found).
                The result populates the matching field; the others stay None.
            retry: Server-side re-attempts on ``target_timeout``, each on a
                fresh connection to the target. Default 0 (one attempt); no
                upper bound on the value, though the server stops re-attempting
                once a job's 6-hour lifetime is spent. Each timed-out attempt
                takes 20-30s on this synchronous call, so aggressive values
                belong on :meth:`bulk`. Distinct from the client's own
                ``max_retries`` (transport retries between you and the API).
            allow_domains: Restrict this request to these domains (and their
                subdomains). Can only narrow your key's own allowlist, never
                widen it.
            deny_patterns: Extra deny globs for this request (matched against
                the hostname and full URL). Added to your key's deny list.
            respect_robots: ``"strict"`` or ``"lax"``. Can upgrade your key's
                setting to ``"strict"`` for this request but never relax it.

        Raises:
            RateLimitError: Monthly plan limit reached.
            PermissionDeniedError: ``plan_not_supported`` (``render_js=True`` on
                Free), or a policy denial — ``domain_not_allowed``,
                ``domain_denied``, ``robots_disallowed``.
            UnprocessableEntityError: ``no_content`` or ``target_timeout``.
            AuthenticationError: Missing or invalid API key.
        """
        payload: dict[str, object] = {
            "url": url, "render_js": render_js, "format": format, "retry": retry,
        }
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = self._request("POST", "/extract", json=payload)
        return ExtractResult.from_response(body)

    def search(
        self,
        query: str,
        *,
        num_results: int = 5,
        render_js: bool = False,
        format: str = "markdown",
        allow_domains: Optional[Iterable[str]] = None,
        deny_patterns: Optional[Iterable[str]] = None,
        respect_robots: Optional[str] = None,
    ) -> SearchResults:
        """Search the web and extract each result. Synchronous.

        One round trip: the query runs against the search provider and the
        result pages are extracted concurrently, returned together with per-page
        ``status`` (a slow or blocked page becomes an error item, never sinks the
        call). Costs ``1 + len(results)`` requests — one for the query, one per
        page returned. Takes the full extraction parameter set — ``format`` and
        the policy overrides apply to every extracted result, exactly as they
        do on :meth:`bulk` and :meth:`crawl`.

        Args:
            query: The search query.
            num_results: How many results to fetch + extract. Clamped to 1..10
                server-side (a search is one synchronous call, so it's capped).
            render_js: Use Playwright to render JS-heavy result pages.
            format: Output format for every result — see :meth:`extract`.
            allow_domains: Narrow-only compliance override — see :meth:`extract`.
            deny_patterns: Narrow-only compliance override — see :meth:`extract`.
            respect_robots: ``"strict"`` or ``"lax"`` — see :meth:`extract`.

        Raises:
            PermissionDeniedError: ``plan_not_supported`` — search requires a
                Pro, Growth, or Enterprise plan.
            RateLimitError: This search would exceed your remaining monthly quota.
            InternalServerError: The search provider is unconfigured or
                unreachable — ``.code == "service_unavailable"`` (retryable).
        """
        payload: dict[str, object] = {
            "query": query, "num_results": num_results,
            "render_js": render_js, "format": format,
        }
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = self._request("POST", "/search", json=payload)
        return SearchResults.from_response(body)

    def bulk(
        self,
        urls: Iterable[str],
        *,
        render_js: bool = False,
        format: str = "markdown",
        retry: int = 0,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
        idempotency_key: Optional[str] = None,
        allow_domains: Optional[Iterable[str]] = None,
        deny_patterns: Optional[Iterable[str]] = None,
        respect_robots: Optional[str] = None,
    ) -> BulkJob:
        """Submit a batch of URLs for concurrent extraction.

        Returns immediately with ``status="queued"``. Poll with
        :meth:`get_job` or block with :meth:`wait_for_job` to collect results
        — OR pass ``webhook_url`` to skip polling entirely.

        An ``Idempotency-Key`` is generated per call and reused across the
        SDK's internal retries (see ``max_retries``), so a connection blip
        replays the original job rather than enqueuing a second one.

        Args:
            urls: The URLs to extract from.
            render_js: Use Playwright to render JS-heavy pages.
            format: Output format — ``"markdown"`` (default), ``"json"``
                (typed heading/paragraph/list/code blocks), ``"chunks"``
                (contiguous 500-token windows for embedding), ``"html"`` (the
                raw fetched HTML) or ``"links"`` (every http(s) link found).
                The result populates the matching field; the others stay None.
            retry: Per-URL server-side re-attempts on ``target_timeout`` —
                see :meth:`extract`. The async workers absorb the retry time,
                so this is the natural home for aggressive values.
            webhook_url: HTTPS URL to receive a signed POST when the job
                finishes. Use :func:`wellmarked.verify_webhook` to verify
                deliveries on the receiving side.
            webhook_include_results: When ``True``, the webhook payload
                includes the full ``results`` array inline (capped at
                ~5 MB; over the cap the payload falls back to the thin
                shape with ``results_truncated_for_size=true``). When
                ``False`` (default), the payload carries only metadata
                and a ``results_url`` pointing at :meth:`get_job`.
            idempotency_key: Sent as the ``Idempotency-Key`` header.
                Generated automatically when omitted, and reused across the
                SDK's internal retries. **Pass your own to survive anything
                the SDK can't retry for you** — a process crash, or your code
                catching the error and calling ``bulk()`` again; that second
                call mints a *new* key and gets a *second* job unless you
                reuse a stable one. Same key + same body replays the original
                job; same key + a different body raises
                ``idempotency_key_reuse``. Records expire after 6 hours,
                matching the job TTL. The body must match byte-for-byte —
                reordering ``urls`` counts as a different request.
            allow_domains, deny_patterns, respect_robots: Per-request
                compliance overrides — see :meth:`extract`. Because bulk
                enforces policy per URL, a denial surfaces as a **per-item
                error** (in ``results[].error``), not a raised exception.

        Raises:
            PermissionDeniedError: ``plan_not_supported`` — ``render_js=True``
                on the Free plan.
            UnprocessableEntityError: ``bulk_cap_exceeded`` (5 on Free, 50 on
                Pro, 200 on Growth), ``webhook_url_invalid``, or
                ``idempotency_key_reuse``.
            RateLimitError: Submitting this batch would exceed your remaining
                monthly quota.
        """
        url_list = list(urls)
        if not url_list:
            raise ValueError("bulk() requires at least one URL.")
        payload: dict[str, object] = {
            "urls": url_list, "render_js": render_js, "format": format, "retry": retry,
        }
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = self._request(
            "POST", "/bulk", json=payload,
            headers={"Idempotency-Key": idempotency_key or new_idempotency_key()},
        )
        return BulkJob.from_response(body)

    @staticmethod
    def _job_from_body(body: dict[str, Any]) -> Union[BulkJob, CrawlJob]:
        """Build the right job type from a ``/jobs/{id}`` body, using ``kind``."""
        if body.get("kind") == "crawl":
            return CrawlJob.from_response(body)
        return BulkJob.from_response(body)

    def get_job(self, job_id: str) -> Union[BulkJob, CrawlJob]:
        """Polymorphic job lookup — works for both bulk and crawl jobs, in
        ONE call.

        ``GET /jobs/{job_id}`` resolves either kind and returns the superset
        shape; the ``kind`` discriminator says which you got. Returns
        :class:`BulkJob` or :class:`CrawlJob` accordingly.

        This used to call ``GET /bulk/{job_id}`` purely to read ``kind``,
        then re-fetch ``GET /crawl/{job_id}`` for the fields the bulk shape
        omits (``truncated``, ``truncated_reason``, per-item ``depth``).
        That cost two round trips per crawl poll, and — because ``/bulk/*``
        requires the ``bulk`` scope — a key scoped to ``crawl`` alone got a
        403 on the discovery call and could never poll its own job.
        ``/jobs/{id}`` requires neither scope.

        Use ``isinstance(job, CrawlJob)`` or ``job.kind == "crawl"`` to
        branch on crawl-specific behavior. The shared interface
        (``status``, ``completed``, ``total``, ``results``, ``done``)
        works on either type.

        Jobs are retained for 6 hours after completion.
        """
        return self._job_from_body(self._request("GET", f"/jobs/{job_id}"))

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = 300.0,
    ) -> Union[BulkJob, CrawlJob]:
        """Block until a job reaches ``status="done"`` (or timeout). Works
        for both bulk and crawl jobs.

        Every poll goes to ``/jobs/{id}``, which answers for either kind, so
        there is no dispatch round-trip to pay and no scope to guess — the
        loop reads ``kind`` off each body.

        Args:
            job_id: The job id returned from :meth:`bulk` or :meth:`crawl`.
            poll_interval: Seconds to sleep between polls.
            timeout: Total seconds to wait. ``None`` waits forever.

        Raises:
            TimeoutError: The job didn't finish before ``timeout`` elapsed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        job: Union[BulkJob, CrawlJob] = self.get_job(job_id)
        while not job.done:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status}, {job.completed}/{job.total})"
                )
            time.sleep(poll_interval)
            job = self._job_from_body(self._request("GET", f"/jobs/{job_id}"))
        return job

    def crawl(
        self,
        url: str,
        *,
        depth: int = 1,
        render_js: bool = False,
        format: str = "markdown",
        retry: int = 0,
        max_pages: Optional[int] = None,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
        idempotency_key: Optional[str] = None,
        allow_domains: Optional[Iterable[str]] = None,
        deny_patterns: Optional[Iterable[str]] = None,
        respect_robots: Optional[str] = None,
    ) -> CrawlJob:
        """Crawl a site starting from ``url``, BFS to ``depth``.

        Returns immediately with ``status="queued"``. Use :meth:`get_job`
        to poll, or :meth:`wait_for_job` to block until done — both
        handle crawl and bulk job_ids transparently. Pass ``webhook_url``
        to receive a signed POST when the job finishes instead of polling.

        Retry-safe by default — see :meth:`bulk` for how ``idempotency_key``
        behaves. It matters at least as much here: a crawl bills per page, so
        an accidental second crawl of the same site bills every page twice.

        Plan caps:
            * Free → ``PermissionDeniedError`` (``plan_not_supported``)
            * Pro → max depth 5, up to 2,000 pages per crawl
            * Growth → max depth 10, up to 10,000 pages per crawl
            * Enterprise → unlimited depth and pages

        See :meth:`bulk` for the meaning of ``webhook_url``,
        ``webhook_include_results``, ``idempotency_key``, and ``retry``
        (per-page timeout re-attempts). ``max_pages`` caps how many pages
        this crawl may bill — it can only narrow your plan's page cap, never
        widen it. The compliance overrides ``allow_domains`` /
        ``deny_patterns`` / ``respect_robots`` work as in :meth:`extract`;
        as with bulk, a per-page policy denial surfaces in
        ``results[].error`` rather than raising.

        Raises:
            PermissionDeniedError: ``plan_not_supported`` (Free tier).
            UnprocessableEntityError: ``crawl_depth_exceeded``,
                ``webhook_url_invalid``, or ``idempotency_key_reuse``.
        """
        if depth < 0:
            raise ValueError("depth must be >= 0.")
        payload: dict[str, object] = {
            "url": url, "depth": depth, "render_js": render_js, "format": format,
            "retry": retry,
        }
        if max_pages is not None:
            payload["max_pages"] = max_pages
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = self._request(
            "POST", "/crawl", json=payload,
            headers={"Idempotency-Key": idempotency_key or new_idempotency_key()},
        )
        return CrawlJob.from_response(body)

    # ── Custom headers ────────────────────────────────────────────────────────

    def set_header(self, name: str, value: str) -> None:
        """Add or replace a per-request header for the rest of this client's life.

        Authorization / Content-Type / Accept are reserved — calls that try
        to set those are silently ignored. To rotate the bearer token, use
        :meth:`rotate_key`.
        """
        if name.lower() in {"authorization", "content-type", "accept"}:
            return
        self._extra_headers[name] = value
        self._client.headers[name] = value

    def remove_header(self, name: str) -> None:
        """Remove a header previously added via ``headers=`` or :meth:`set_header`."""
        self._extra_headers.pop(name, None)
        self._client.headers.pop(name, None)

    def get_usage(self) -> Usage:
        """Return your usage for the current billing period.

        Does not count toward your monthly quota.
        """
        body = self._request("GET", "/usage")
        return Usage.from_response(body)

    def rotate_key(self) -> RotatedKey:
        """Mint a new API key. The current key is invalidated immediately.

        The new raw key is in the returned ``api_key`` field — store it before
        discarding the result. There is no recovery flow.

        Does not count toward your monthly quota.
        """
        body = self._request("POST", "/keys/rotate")
        rotated = RotatedKey.from_response(body)
        # Rotate the auth header on our own client so subsequent calls work.
        if rotated.api_key:
            self._api_key = rotated.api_key
            self._client.headers["Authorization"] = f"Bearer {rotated.api_key}"
        return rotated

    def rotate_webhook_secret(self) -> RotatedWebhookSecret:
        """Mint a new webhook signing secret. The current secret is
        invalidated immediately.

        Use this when you've lost the secret returned in the
        ``webhook_signing_secret`` field of an earlier :meth:`bulk` /
        :meth:`crawl` response, or when you suspect compromise. Any
        deliveries already queued for retry will be signed with the NEW
        secret on their next attempt.

        Does not count toward your monthly quota.
        """
        body = self._request("POST", "/webhook/rotate")
        return RotatedWebhookSecret.from_response(body)

    # ── Key management (scoped keys) ──────────────────────────────────────────

    def create_key(self, scopes: Iterable[str], *, name: str = "default") -> CreatedKey:
        """Mint a new scoped API key on this account.

        Args:
            scopes: A non-empty subset of ``extract``, ``bulk``, ``crawl``,
                ``keys``. Your calling key can only grant scopes it holds.
            name: A label for the key (shown in :meth:`list_keys`).

        The raw key is in the returned ``api_key`` — store it, it's shown once.
        Requires the ``keys`` scope. Doesn't count toward your quota.

        Raises:
            PermissionDeniedError: ``insufficient_scope`` — your key lacks
                ``keys``, or you asked for a scope it doesn't hold.
            UnprocessableEntityError: ``invalid_request`` — empty or unknown
                scopes.
        """
        body = self._request(
            "POST", "/keys", json={"scopes": list(scopes), "name": name},
        )
        return CreatedKey.from_response(body)

    def list_keys(self) -> list[ApiKeyInfo]:
        """List this account's keys (metadata only — never the raw values),
        including revoked ones (``revoked_at`` set). Requires the ``keys``
        scope. Doesn't count toward your quota."""
        body = self._request("GET", "/keys")
        return [ApiKeyInfo.from_dict(k) for k in (body.get("keys") or [])]

    def revoke_key(self, key_id: str) -> RevokedKey:
        """Revoke a key by id — it stops authenticating immediately. Idempotent.
        Requires the ``keys`` scope. Doesn't count toward your quota.

        Raises:
            NotFoundError: ``key_not_found`` — no such key on this account.
        """
        body = self._request("DELETE", f"/keys/{key_id}")
        return RevokedKey.from_response(body)

    # ── Audit log ─────────────────────────────────────────────────────────────

    def get_logs(self, *, limit: int = 50, offset: int = 0) -> LogsPage:
        """Return this account's request history, newest first — the audit
        trail (which key ran each call and how its policy decided). Own rows
        only. Paginate via ``offset += limit`` while ``has_more`` is True.
        Doesn't count toward your quota."""
        body = self._request("GET", f"/logs?limit={int(limit)}&offset={int(offset)}")
        return LogsPage.from_response(body)

    # ── Transport ─────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        # Build absolute URLs ourselves rather than relying on httpx's base_url
        # join — one obvious place constructs every request URL.
        url = f"{self._base_url}{path}"
        extra = sanitize_headers(headers)

        # Retries reuse `extra` unchanged, so every attempt carries the SAME
        # Idempotency-Key and the API collapses them into one job. See
        # _base.is_safe_to_replay for why POST /extract is excluded.
        attempts = self._max_retries + 1 if is_safe_to_replay(method, extra) else 1
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            if attempt:
                time.sleep(backoff_seconds(attempt))
            try:
                # Per-request headers are passed to httpx, never written onto
                # the shared client — see _base.sanitize_headers.
                response = self._client.request(method, url, json=json, headers=extra)
            except httpx.HTTPError as exc:
                last_exc = wrap_transport_error(exc)
                continue

            # 5xx is the other ambiguous case: the API may have committed the
            # job before failing. 4xx is deterministic — replaying reproduces
            # it, so don't waste the caller's time.
            if response.status_code >= 500 and attempt < attempts - 1:
                continue

            try:
                assert response.content
                body = response.json()
            except (AssertionError, ValueError):
                body = None

            # httpx Headers is dict-like for our purposes; pass it through so
            # parse_response can read Retry-After-Ms on rate-limit 429s.
            return parse_response(
                response.status_code, body, headers=dict(response.headers),
            )

        assert last_exc is not None  # only reachable via the except branch
        raise last_exc
