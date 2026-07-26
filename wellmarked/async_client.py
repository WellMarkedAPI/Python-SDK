"""Asynchronous WellMarked client."""
from __future__ import annotations

import asyncio
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


class AsyncWellMarked:
    """Asynchronous client for the WellMarked API.

    Mirrors :class:`wellmarked.WellMarked` exactly; every endpoint method is
    a coroutine. Use as an async context manager::

        from wellmarked import AsyncWellMarked

        async with AsyncWellMarked(api_key="wm_...") as wm:
            result = await wm.extract("https://example.com/article")
            print(result.markdown)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Async equivalent of :class:`wellmarked.WellMarked`. See its docstring
        for the full parameter description, including ``headers`` and
        ``max_retries``."""
        self._api_key = resolve_api_key(api_key)
        self._base_url = DEFAULT_BASE_URL
        self._max_retries = max(0, max_retries)
        self._extra_headers: dict[str, str] = dict(headers or {})
        merged = merge_headers(self._api_key, self._extra_headers)
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self._base_url,
            headers=merged,
            timeout=timeout,
        )

    # ── Self-registration ─────────────────────────────────────────────────────

    @classmethod
    async def register(
        cls,
        email: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> RegisteredAccount:
        """Self-register for a free, extract-only API key. See
        :meth:`WellMarked.register`. Not retried (registration isn't idempotent)."""
        url = f"{DEFAULT_BASE_URL}/register"
        client = httpx.AsyncClient(timeout=timeout)
        try:
            try:
                response = await client.post(url, json={"email": email})
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
            await client.aclose()
        return RegisteredAccount.from_response(data)

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncWellMarked":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying connection pool. See :meth:`WellMarked.close`."""
        await self._client.aclose()

    # ── Endpoints ─────────────────────────────────────────────────────────────

    async def extract(
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
        """Extract clean Markdown from a single URL. See :meth:`WellMarked.extract`."""
        payload: dict[str, object] = {
            "url": url, "render_js": render_js, "format": format, "retry": retry,
        }
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = await self._request("POST", "/extract", json=payload)
        return ExtractResult.from_response(body)

    async def search(
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
        """Search the web and extract each result. See
        :meth:`WellMarked.search` — takes the full extraction parameter set."""
        payload: dict[str, object] = {
            "query": query, "num_results": num_results,
            "render_js": render_js, "format": format,
        }
        payload.update(policy_overrides(allow_domains, deny_patterns, respect_robots))
        body = await self._request("POST", "/search", json=payload)
        return SearchResults.from_response(body)

    async def bulk(
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
        """Submit a batch of URLs for concurrent extraction. See :meth:`WellMarked.bulk`."""
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
        body = await self._request(
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

    async def get_job(self, job_id: str) -> Union[BulkJob, CrawlJob]:
        """Polymorphic job lookup — works for both bulk and crawl jobs, in
        ONE call to ``GET /jobs/{job_id}``.

        See :meth:`wellmarked.WellMarked.get_job` for why this replaced the
        old ``/bulk`` discovery call plus ``/crawl`` re-fetch.
        """
        return self._job_from_body(await self._request("GET", f"/jobs/{job_id}"))

    async def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = 300.0,
    ) -> Union[BulkJob, CrawlJob]:
        """Await until a job reaches ``status="done"`` (or timeout). Works
        for both bulk and crawl jobs — same single-call polling as the sync
        client. See :meth:`wellmarked.WellMarked.wait_for_job`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        job: Union[BulkJob, CrawlJob] = await self.get_job(job_id)
        while not job.done:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status}, {job.completed}/{job.total})"
                )
            await asyncio.sleep(poll_interval)
            job = self._job_from_body(await self._request("GET", f"/jobs/{job_id}"))
        return job

    async def crawl(
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
        """Crawl a site starting from ``url``. See :meth:`WellMarked.crawl`."""
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
        body = await self._request(
            "POST", "/crawl", json=payload,
            headers={"Idempotency-Key": idempotency_key or new_idempotency_key()},
        )
        return CrawlJob.from_response(body)

    # ── Custom headers ────────────────────────────────────────────────────────

    def set_header(self, name: str, value: str) -> None:
        """Add or replace a per-request header. See :meth:`WellMarked.set_header`."""
        if name.lower() in {"authorization", "content-type", "accept"}:
            return
        self._extra_headers[name] = value
        self._client.headers[name] = value

    def remove_header(self, name: str) -> None:
        """Remove a previously-added header."""
        self._extra_headers.pop(name, None)
        self._client.headers.pop(name, None)

    async def get_usage(self) -> Usage:
        """Return your usage for the current billing period."""
        body = await self._request("GET", "/usage")
        return Usage.from_response(body)

    async def rotate_key(self) -> RotatedKey:
        """Mint a new API key. See :meth:`WellMarked.rotate_key`."""
        body = await self._request("POST", "/keys/rotate")
        rotated = RotatedKey.from_response(body)
        if rotated.api_key:
            self._api_key = rotated.api_key
            self._client.headers["Authorization"] = f"Bearer {rotated.api_key}"
        return rotated

    async def rotate_webhook_secret(self) -> RotatedWebhookSecret:
        """Mint a new webhook signing secret. See :meth:`WellMarked.rotate_webhook_secret`."""
        body = await self._request("POST", "/webhook/rotate")
        return RotatedWebhookSecret.from_response(body)

    # ── Key management (scoped keys) ──────────────────────────────────────────

    async def create_key(self, scopes: Iterable[str], *, name: str = "default") -> CreatedKey:
        """Mint a new scoped API key. See :meth:`WellMarked.create_key`."""
        body = await self._request(
            "POST", "/keys", json={"scopes": list(scopes), "name": name},
        )
        return CreatedKey.from_response(body)

    async def list_keys(self) -> list[ApiKeyInfo]:
        """List this account's keys (metadata only). See :meth:`WellMarked.list_keys`."""
        body = await self._request("GET", "/keys")
        return [ApiKeyInfo.from_dict(k) for k in (body.get("keys") or [])]

    async def revoke_key(self, key_id: str) -> RevokedKey:
        """Revoke a key by id. See :meth:`WellMarked.revoke_key`."""
        body = await self._request("DELETE", f"/keys/{key_id}")
        return RevokedKey.from_response(body)

    # ── Audit log ─────────────────────────────────────────────────────────────

    async def get_logs(self, *, limit: int = 50, offset: int = 0) -> LogsPage:
        """Return this account's request history. See :meth:`WellMarked.get_logs`."""
        body = await self._request("GET", f"/logs?limit={int(limit)}&offset={int(offset)}")
        return LogsPage.from_response(body)

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: object = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        # See WellMarked._request — this mirrors the sync client exactly,
        # including the replay-safety rule and the backoff schedule.
        url = f"{self._base_url}{path}"
        extra = sanitize_headers(headers)
        attempts = self._max_retries + 1 if is_safe_to_replay(method, extra) else 1
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(backoff_seconds(attempt))
            try:
                # Per-request headers go to httpx, never onto the shared
                # client: this client is used concurrently, so mutating
                # client.headers would leak one call's headers into another's.
                response = await self._client.request(
                    method, url, json=json, headers=extra,
                )
            except httpx.HTTPError as exc:
                last_exc = wrap_transport_error(exc)
                continue

            if response.status_code >= 500 and attempt < attempts - 1:
                continue

            try:
                assert response.content
                body = response.json()
            except (AssertionError, ValueError):
                body = None
            break
        else:
            assert last_exc is not None
            raise last_exc

        # httpx Headers is dict-like for our purposes; pass it through so
        # parse_response can read Retry-After-Ms on rate-limit 429s.
        return parse_response(response.status_code, body, headers=dict(response.headers))
