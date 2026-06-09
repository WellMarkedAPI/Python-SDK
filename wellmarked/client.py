"""Synchronous WellMarked client."""
from __future__ import annotations

import time
from typing import Iterable, Optional, Union, Any

import httpx

from ._base import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    merge_headers,
    parse_response,
    resolve_api_key,
    wrap_transport_error,
)
from .models import BulkJob, CrawlJob, ExtractResult, RotatedKey, RotatedWebhookSecret, Usage


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
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.Client] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Create a sync client.

        Args:
            api_key: Your WellMarked API key (``wm_...``). Falls back to the
                ``WELLMARKED_API_KEY`` environment variable.
            base_url: API base URL. Override for testing.
            timeout: Per-request timeout, seconds.
            http_client: Bring your own ``httpx.Client`` (custom transport,
                proxy, shared pool). The SDK won't close it on ``__exit__``
                when you pass one.
            headers: Extra headers sent on every request — useful for adding
                an internal correlation id, a custom user agent suffix, etc.
                Authorization / Content-Type / Accept are reserved and silently
                ignored if passed (the SDK manages those itself).
        """
        self._api_key = resolve_api_key(api_key)
        self._base_url = base_url.rstrip("/")
        self._extra_headers: dict[str, str] = dict(headers or {})
        merged = merge_headers(self._api_key, self._extra_headers)
        self._owns_client = http_client is None
        self._client: httpx.Client = http_client or httpx.Client(
            base_url=self._base_url,
            headers=merged,
            timeout=timeout,
        )
        # When the caller supplies their own client, only patch the auth
        # header (and any extras they asked us to add) — don't replace
        # headers they may have set deliberately.
        if not self._owns_client:
            self._client.headers.update(merged)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "WellMarked":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool.

        Only closes the HTTP client if the SDK created it. If you passed your
        own ``http_client``, you remain responsible for closing it.
        """
        if self._owns_client:
            self._client.close()

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def extract(self, url: str, *, render_js: bool = False) -> ExtractResult:
        """Extract clean Markdown from a single URL.

        Args:
            url: The URL to extract content from.
            render_js: Use Playwright to render JS-heavy pages. Requires a
                Pro/Enterprise plan AND ``ENABLE_JS_RENDERING=true`` on the
                API instance.

        Raises:
            RateLimitError: Monthly plan limit reached.
            UnprocessableEntityError: ``no_content``, ``target_timeout``, or
                ``js_rendering_disabled``.
            AuthenticationError: Missing or invalid API key.
        """
        body = self._request(
            "POST", "/extract", json={"url": url, "render_js": render_js}
        )
        return ExtractResult.from_response(body)

    def bulk(
        self,
        urls: Iterable[str],
        *,
        render_js: bool = False,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
    ) -> BulkJob:
        """Submit a batch of URLs for concurrent extraction.

        Returns immediately with ``status="queued"``. Poll with
        :meth:`get_job` or block with :meth:`wait_for_job` to collect results
        — OR pass ``webhook_url`` to skip polling entirely.

        Args:
            urls: The URLs to extract from.
            render_js: Use Playwright to render JS-heavy pages.
            webhook_url: HTTPS URL to receive a signed POST when the job
                finishes. Use :func:`wellmarked.verify_webhook` to verify
                deliveries on the receiving side.
            webhook_include_results: When ``True``, the webhook payload
                includes the full ``results`` array inline (capped at
                ~5 MB; over the cap the payload falls back to the thin
                shape with ``results_truncated_for_size=true``). When
                ``False`` (default), the payload carries only metadata
                and a ``results_url`` pointing at :meth:`get_job`.

        Raises:
            PermissionDeniedError: ``plan_not_supported`` (Free tier).
            UnprocessableEntityError: ``bulk_cap_exceeded`` (50 on Pro,
                200 on Growth) or ``webhook_url_invalid``.
            RateLimitError: Submitting this batch would exceed your remaining
                monthly quota.
        """
        url_list = list(urls)
        if not url_list:
            raise ValueError("bulk() requires at least one URL.")
        payload: dict[str, object] = {"urls": url_list, "render_js": render_js}
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        body = self._request("POST", "/bulk", json=payload)
        return BulkJob.from_response(body)

    def get_job(self, job_id: str) -> Union[BulkJob, CrawlJob]:
        """Polymorphic job lookup — works for both bulk and crawl jobs.

        Calls ``GET /bulk/{job_id}`` first, then inspects the response's
        ``kind`` discriminator field. If the job is actually a crawl, a
        second request to ``GET /crawl/{job_id}`` fetches the full crawl
        shape (with per-item depth and the truncated flags). Returns
        :class:`BulkJob` or :class:`CrawlJob` accordingly.

        Use ``isinstance(job, CrawlJob)`` or ``job.kind == "crawl"`` to
        branch on crawl-specific behavior. The shared interface
        (``status``, ``completed``, ``total``, ``results``, ``done``)
        works on either type.

        Jobs are retained for 6 hours after completion.
        """
        body = self._request("GET", f"/bulk/{job_id}")
        # /bulk/{id} answers for any job_id today (the endpoint just
        # serializes results in the bulk shape regardless of stored
        # job_type — see api/routes/bulk.py). The `kind` field tells
        # us whether we got a bulk-shaped response of a crawl job; if
        # so, re-fetch via /crawl/{id} for the proper shape.
        if body.get("kind") == "crawl":
            body = self._request("GET", f"/crawl/{job_id}")
            return CrawlJob.from_response(body)
        return BulkJob.from_response(body)

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = 300.0,
    ) -> Union[BulkJob, CrawlJob]:
        """Block until a job reaches ``status="done"`` (or timeout). Works
        for both bulk and crawl jobs.

        The first call uses the polymorphic :meth:`get_job` to discover
        the job's kind. Subsequent polls go directly to the typed
        endpoint, so a crawl job only pays the dispatch round-trip once.

        Args:
            job_id: The job id returned from :meth:`bulk` or :meth:`crawl`.
            poll_interval: Seconds to sleep between polls.
            timeout: Total seconds to wait. ``None`` waits forever.

        Raises:
            TimeoutError: The job didn't finish before ``timeout`` elapsed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        # First call: figure out which endpoint owns this job.
        job: Union[BulkJob, CrawlJob] = self.get_job(job_id)
        is_crawl = isinstance(job, CrawlJob)
        while not job.done:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status}, {job.completed}/{job.total})"
                )
            time.sleep(poll_interval)
            # Subsequent polls hit the typed endpoint directly — skips the
            # /bulk + /crawl dispatch get_job does for crawl jobs.
            path = f"/crawl/{job_id}" if is_crawl else f"/bulk/{job_id}"
            body = self._request("GET", path)
            job = CrawlJob.from_response(body) if is_crawl else BulkJob.from_response(body)
        return job

    def crawl(
        self,
        url: str,
        *,
        depth: int = 1,
        render_js: bool = False,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
    ) -> CrawlJob:
        """Crawl a site starting from ``url``, BFS to ``depth``.

        Returns immediately with ``status="queued"``. Use :meth:`get_job`
        to poll, or :meth:`wait_for_job` to block until done — both
        handle crawl and bulk job_ids transparently. Pass ``webhook_url``
        to receive a signed POST when the job finishes instead of polling.

        Plan caps:
            * Free → ``PermissionDeniedError`` (``plan_not_supported``)
            * Pro → max depth 5, up to 2,000 pages per crawl
            * Growth → max depth 10, up to 10,000 pages per crawl
            * Enterprise → unlimited depth and pages

        See :meth:`bulk` for the meaning of ``webhook_url`` and
        ``webhook_include_results``.

        Raises:
            PermissionDeniedError: ``plan_not_supported`` (Free tier).
            UnprocessableEntityError: ``crawl_depth_exceeded`` or
                ``webhook_url_invalid``.
        """
        if depth < 0:
            raise ValueError("depth must be >= 0.")
        payload: dict[str, object] = {
            "url": url, "depth": depth, "render_js": render_js,
        }
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        body = self._request("POST", "/crawl", json=payload)
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

    # ── Transport ─────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, json: object = None) -> Any:
        # Build absolute URLs ourselves rather than relying on httpx's base_url
        # join. That way a user-supplied http_client without a base_url still
        # works — important when the caller wants a custom transport/proxy.
        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(method, url, json=json)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc) from exc

        try:
            assert response.content
            body = response.json()
        except (AssertionError, ValueError):
            body = None

        # httpx Headers is dict-like for our purposes; pass it through so
        # parse_response can read Retry-After-Ms on rate-limit 429s.
        return parse_response(response.status_code, body, headers=dict(response.headers))
