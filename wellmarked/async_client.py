"""Asynchronous WellMarked client."""
from __future__ import annotations

import asyncio
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
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.AsyncClient] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Async equivalent of :class:`wellmarked.WellMarked`. See its docstring
        for the full parameter description, including ``headers``."""
        self._api_key = resolve_api_key(api_key)
        self._base_url = base_url.rstrip("/")
        self._extra_headers: dict[str, str] = dict(headers or {})
        merged = merge_headers(self._api_key, self._extra_headers)
        self._owns_client = http_client is None
        self._client: httpx.AsyncClient = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers=merged,
            timeout=timeout,
        )
        if not self._owns_client:
            self._client.headers.update(merged)

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncWellMarked":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying connection pool. See :meth:`WellMarked.close`."""
        if self._owns_client:
            await self._client.aclose()

    # ── Endpoints ─────────────────────────────────────────────────────────────

    async def extract(self, url: str, *, render_js: bool = False) -> ExtractResult:
        """Extract clean Markdown from a single URL. See :meth:`WellMarked.extract`."""
        body = await self._request(
            "POST", "/extract", json={"url": url, "render_js": render_js}
        )
        return ExtractResult.from_response(body)

    async def bulk(
        self,
        urls: Iterable[str],
        *,
        render_js: bool = False,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
    ) -> BulkJob:
        """Submit a batch of URLs for concurrent extraction. See :meth:`WellMarked.bulk`."""
        url_list = list(urls)
        if not url_list:
            raise ValueError("bulk() requires at least one URL.")
        payload: dict[str, object] = {"urls": url_list, "render_js": render_js}
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        body = await self._request("POST", "/bulk", json=payload)
        return BulkJob.from_response(body)

    async def get_job(self, job_id: str) -> Union[BulkJob, CrawlJob]:
        """Polymorphic job lookup — works for both bulk and crawl jobs.

        See :meth:`wellmarked.WellMarked.get_job` for the full
        explanation of the kind-discriminator + optional re-fetch model.
        """
        body = await self._request("GET", f"/bulk/{job_id}")
        if body.get("kind") == "crawl":
            body = await self._request("GET", f"/crawl/{job_id}")
            return CrawlJob.from_response(body)
        return BulkJob.from_response(body)

    async def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = 300.0,
    ) -> Union[BulkJob, CrawlJob]:
        """Await until a job reaches ``status="done"`` (or timeout). Works
        for both bulk and crawl jobs — same polymorphic dispatch as the
        sync client. See :meth:`wellmarked.WellMarked.wait_for_job`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        job: Union[BulkJob, CrawlJob] = await self.get_job(job_id)
        is_crawl = isinstance(job, CrawlJob)
        while not job.done:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Job {job_id} did not finish within {timeout}s "
                    f"(last status: {job.status}, {job.completed}/{job.total})"
                )
            await asyncio.sleep(poll_interval)
            # Subsequent polls hit the typed endpoint directly — skips the
            # /bulk + /crawl dispatch get_job does for crawl jobs.
            path = f"/crawl/{job_id}" if is_crawl else f"/bulk/{job_id}"
            body = await self._request("GET", path)
            job = CrawlJob.from_response(body) if is_crawl else BulkJob.from_response(body)
        return job

    async def crawl(
        self,
        url: str,
        *,
        depth: int = 1,
        render_js: bool = False,
        webhook_url: Optional[str] = None,
        webhook_include_results: bool = False,
    ) -> CrawlJob:
        """Crawl a site starting from ``url``. See :meth:`WellMarked.crawl`."""
        if depth < 0:
            raise ValueError("depth must be >= 0.")
        payload: dict[str, object] = {
            "url": url, "depth": depth, "render_js": render_js,
        }
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
            payload["webhook_include_results"] = webhook_include_results
        body = await self._request("POST", "/crawl", json=payload)
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

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, *, json: object = None) -> Any:
        # See WellMarked._request for why we build absolute URLs ourselves.
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.request(method, url, json=json)
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
