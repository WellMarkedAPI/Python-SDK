"""Mocked-transport tests for the sync and async clients."""
from __future__ import annotations

import httpx
import pytest
import respx

from wellmarked import (
    APIConnectionError,
    AsyncWellMarked,
    AuthenticationError,
    BulkItem,
    CrawlJob,
    ExtractionMeta,
    ExtractResult,
    PermissionDeniedError,
    RateLimitError,
    SearchResult,
    SearchResults,
    UnprocessableEntityError,
    WellMarked,
    WellMarkedError,
)

API_KEY = "wm_" + "a" * 40
BASE_URL = "https://api.wellmarked.io"


# ── Sync ──────────────────────────────────────────────────────────────────────

@respx.mock
def test_extract_success() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "markdown": "## Hello",
                "metadata": {
                    "title": "Hello",
                    "author": "Me",
                    "date": "2026-05-01",
                    "url": "https://example.com",
                    "retrieved_at": "2026-05-16T12:34:56+00:00",
                },
                "request_id": "11111111-1111-1111-1111-111111111111",
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        result = wm.extract("https://example.com")

    assert result.markdown == "## Hello"
    assert result.metadata.title == "Hello"
    assert result.metadata.author == "Me"
    assert result.metadata.retrieved_at is not None
    assert result.request_id == "11111111-1111-1111-1111-111111111111"
    # Quota info is intentionally NOT on extract results — comes from get_usage.
    assert not hasattr(result, "rate_limit")


@respx.mock
def test_rate_limit_error_carries_retry_after() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"code": "rate_limit_exceeded", "message": "Quota hit.", "retry_after": 1209600}},
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(RateLimitError) as exc_info:
            wm.extract("https://example.com")

    assert exc_info.value.code == "rate_limit_exceeded"
    assert exc_info.value.retry_after == 1209600
    assert exc_info.value.status_code == 429
    # Monthly-quota rate limits don't get a sub-second hint.
    assert exc_info.value.retry_after_ms is None


@respx.mock
def test_rate_limit_too_fast_surfaces_retry_after_ms() -> None:
    """The per-second cap (rate_limit_too_fast) returns a Retry-After-Ms
    header; the SDK exposes it as RateLimitError.retry_after_ms so
    callers can sleep precisely instead of rounding up to a full second."""
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": "rate_limit_too_fast",
                    "message": "Request rate exceeded.",
                    "retry_after": 1,
                },
            },
            headers={"Retry-After": "1", "Retry-After-Ms": "43"},
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(RateLimitError) as exc_info:
            wm.extract("https://example.com")

    assert exc_info.value.code == "rate_limit_too_fast"
    assert exc_info.value.retry_after == 1
    assert exc_info.value.retry_after_ms == 43
    assert exc_info.value.status_code == 429


@respx.mock
def test_auth_error_on_401() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "invalid_api_key", "message": "Bad key."}}
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(AuthenticationError) as exc_info:
            wm.extract("https://example.com")

    assert exc_info.value.code == "invalid_api_key"


@respx.mock
def test_unprocessable_for_target_timeout() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            422, json={"error": {"code": "target_timeout", "message": "Timed out."}}
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(UnprocessableEntityError) as exc_info:
            wm.extract("https://example.com")

    assert exc_info.value.code == "target_timeout"


@respx.mock
def test_bulk_returns_queued_job() -> None:
    respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "1c4f9a02-0000-0000-0000-000000000000",
                "status": "queued",
                "total": 2,
                "completed": 0,
                "results": [],
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.bulk(["https://a.example", "https://b.example"])

    assert job.status == "queued"
    assert job.total == 2
    assert not job.done


@respx.mock
def test_bulk_free_tier_plan_not_supported() -> None:
    respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"code": "plan_not_supported", "message": "Upgrade."}},
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(PermissionDeniedError) as exc_info:
            wm.bulk(["https://a.example"])

    assert exc_info.value.code == "plan_not_supported"


@respx.mock
def test_get_usage_is_the_source_of_truth_for_quota() -> None:
    """get_usage() returns plan/period/used/limit/remaining — the only way to
    read quota state from the SDK."""
    respx.get(f"{BASE_URL}/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "plan": "pro",
                "period": "2026-05",
                "used": 1042,
                "limit": 10000,
                "remaining": 8958,
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        usage = wm.get_usage()

    assert usage.plan == "pro"
    assert usage.period == "2026-05"
    assert usage.used == 1042
    assert usage.limit == 10000
    assert usage.remaining == 8958


@respx.mock
def test_rotate_key_updates_auth_header() -> None:
    new_key = "wm_" + "b" * 40
    respx.post(f"{BASE_URL}/keys/rotate").mock(
        return_value=httpx.Response(
            200,
            json={
                "api_key": new_key,
                "rotated_at": "2026-05-13T15:32:00.123456+00:00",
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        rotated = wm.rotate_key()
        # Subsequent requests should carry the new bearer token.
        assert wm._client.headers["Authorization"] == f"Bearer {new_key}"

    assert rotated.api_key == new_key
    assert rotated.rotated_at is not None


def test_missing_api_key_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WELLMARKED_API_KEY", raising=False)
    with pytest.raises(ValueError, match="No API key"):
        WellMarked()


def test_env_var_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WELLMARKED_API_KEY", API_KEY)
    client = WellMarked()
    assert client._api_key == API_KEY
    client.close()


# ── Async ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_async_extract_success() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "markdown": "## Hi",
                "metadata": {"title": "Hi", "author": None, "date": None, "url": "https://example.com"},
                "request_id": "22222222-2222-2222-2222-222222222222",
            },
        )
    )

    async with AsyncWellMarked(api_key=API_KEY) as wm:
        result = await wm.extract("https://example.com")

    assert result.markdown == "## Hi"
    assert result.request_id == "22222222-2222-2222-2222-222222222222"
    assert not hasattr(result, "rate_limit")


@pytest.mark.asyncio
@respx.mock
async def test_async_wait_for_job_polls_until_done() -> None:
    job_id = "1c4f9a02-0000-0000-0000-000000000000"
    route = respx.get(f"{BASE_URL}/bulk/{job_id}")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "job_id": job_id, "status": "processing", "total": 2, "completed": 1,
                "results": [], "created_at": "2026-05-12T14:02:11+00:00",
            },
        ),
        httpx.Response(
            200,
            json={
                "job_id": job_id, "status": "done", "total": 2, "completed": 2,
                "results": [
                    {"url": "https://a.example", "markdown": "## A", "metadata": {"url": "https://a.example"}, "error": None},
                    {"url": "https://b.example", "markdown": None, "metadata": None, "error": "target_timeout"},
                ],
                "created_at": "2026-05-12T14:02:11+00:00",
                "finished_at": "2026-05-12T14:02:14+00:00",
            },
        ),
    ]

    async with AsyncWellMarked(api_key=API_KEY) as wm:
        job = await wm.wait_for_job(job_id, poll_interval=0, timeout=5)

    assert job.done
    assert job.completed == 2
    assert job.results[0].ok
    assert not job.results[1].ok
    assert job.results[1].error == "target_timeout"


# ── Regression: ExtractionMeta surfaces all documented sub-attributes ─────────

def test_extraction_meta_has_all_documented_fields() -> None:
    """The API doc lists url, title, author, date, retrieved_at — every one
    must be a field on the dataclass."""
    meta = ExtractionMeta.from_dict(
        {
            "url": "https://example.com",
            "title": "T",
            "author": "A",
            "date": "2026-05-01",
            "retrieved_at": "2026-05-16T12:34:56+00:00",
        }
    )
    assert meta.url == "https://example.com"
    assert meta.title == "T"
    assert meta.author == "A"
    assert meta.date == "2026-05-01"
    assert meta.retrieved_at is not None
    assert meta.retrieved_at.year == 2026
    assert meta.retrieved_at.month == 5
    assert meta.retrieved_at.day == 16

    # Null title/author/date should be preserved as None, not collapsed.
    # retrieved_at can also be absent (older fixtures) — also None.
    meta_nulls = ExtractionMeta.from_dict(
        {"url": "https://example.com", "title": None, "author": None, "date": None}
    )
    assert meta_nulls.url == "https://example.com"
    assert meta_nulls.title is None
    assert meta_nulls.author is None
    assert meta_nulls.date is None
    assert meta_nulls.retrieved_at is None


# ── Regression: bulk([]) validates client-side ────────────────────────────────

def test_bulk_empty_list_raises_value_error() -> None:
    """Empty URL lists should fail client-side with a clear ValueError, not a
    confusing 422 from the API's pydantic validator."""
    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(ValueError, match="at least one URL"):
            wm.bulk([])


@pytest.mark.asyncio
async def test_async_bulk_empty_list_raises_value_error() -> None:
    async with AsyncWellMarked(api_key=API_KEY) as wm:
        with pytest.raises(ValueError, match="at least one URL"):
            await wm.bulk([])


# ── Regression: user-supplied http_client with no base_url still works ────────

@respx.mock
def test_user_supplied_http_client_without_base_url() -> None:
    """A user passing their own httpx.Client (e.g. for a custom transport)
    shouldn't need to know to set base_url — the SDK builds absolute URLs."""
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "markdown": "## Hi",
                "metadata": {"url": "https://example.com"},
                "request_id": "33333333-3333-3333-3333-333333333333",
            },
        )
    )

    user_client = httpx.Client()  # no base_url
    try:
        wm = WellMarked(api_key=API_KEY, http_client=user_client)
        result = wm.extract("https://example.com")
        assert result.markdown == "## Hi"
    finally:
        user_client.close()


# ── Regression: 2xx with no JSON body raises a clear error ────────────────────

@respx.mock
def test_2xx_with_empty_body_raises_clear_error() -> None:
    """A contract-violating 2xx with no body should raise WellMarkedError,
    not crash with AttributeError on None.get()."""
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(200, content=b"")
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(WellMarkedError, match="no JSON body"):
            wm.extract("https://example.com")


# ── Regression: BulkItem.from_dict handles metadata: {} ───────────────────────

def test_bulk_item_empty_metadata_dict_yields_empty_meta_not_none() -> None:
    """If the API ever returns metadata={}, we should produce an empty
    ExtractionMeta, not silently swap it for None (which would mask a real
    contract change)."""
    item = BulkItem.from_dict(
        {"url": "https://example.com", "markdown": "x", "metadata": {}, "error": None}
    )
    assert item.metadata is not None
    assert isinstance(item.metadata, ExtractionMeta)
    assert item.metadata.url == ""

    # Explicit None still becomes None (the documented failure shape).
    item_failed = BulkItem.from_dict(
        {"url": "https://example.com", "markdown": None, "metadata": None, "error": "target_timeout"}
    )
    assert item_failed.metadata is None


# ── Crawl ─────────────────────────────────────────────────────────────────────

@respx.mock
def test_crawl_returns_queued_job() -> None:
    respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "status": "queued",
                "total": 0,
                "completed": 0,
                "truncated": False,
                "truncated_reason": None,
                "results": [],
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.crawl("https://example.com", depth=2)

    assert isinstance(job, CrawlJob)
    assert job.status == "queued"
    assert not job.truncated
    assert job.truncated_reason is None
    assert not job.done


@respx.mock
def test_crawl_free_tier_plan_not_supported() -> None:
    respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"code": "plan_not_supported", "message": "Upgrade."}},
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(PermissionDeniedError) as exc_info:
            wm.crawl("https://example.com", depth=1)

    assert exc_info.value.code == "plan_not_supported"


@respx.mock
def test_crawl_depth_exceeded_for_pro() -> None:
    respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(
            422,
            json={"error": {"code": "crawl_depth_exceeded", "message": "Pro caps at depth 5."}},
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(UnprocessableEntityError) as exc_info:
            wm.crawl("https://example.com", depth=10)

    assert exc_info.value.code == "crawl_depth_exceeded"


def test_crawl_negative_depth_validates_client_side() -> None:
    """Catch depth<0 before the HTTP call — same posture as bulk([])."""
    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(ValueError, match="depth must be >= 0"):
            wm.crawl("https://example.com", depth=-1)


@respx.mock
def test_get_job_surfaces_crawl_truncation_fields() -> None:
    """Regression: CrawlJob parses every truncation-related field correctly
    when reached through the polymorphic get_job path. /bulk reports the
    job's kind as crawl; the SDK re-fetches /crawl for the full shape."""
    job_id = "9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    respx.get(f"{BASE_URL}/bulk/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 1000, "completed": 1000,
                "results": [],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:02:30+00:00",
            },
        )
    )
    respx.get(f"{BASE_URL}/crawl/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done",
                "total": 1000,
                "completed": 1000,
                "truncated": True,
                "truncated_reason": "page_cap_reached",
                "results": [
                    {
                        "url": "https://example.com",
                        "depth": 0,
                        "markdown": "## Root",
                        "metadata": {"url": "https://example.com"},
                        "error": None,
                    },
                    {
                        "url": "https://example.com/missing",
                        "depth": 1,
                        "markdown": None,
                        "metadata": None,
                        "error": "target_http_error",
                    },
                ],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:02:30+00:00",
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.get_job(job_id)

    assert isinstance(job, CrawlJob)
    assert job.done
    assert job.truncated
    assert job.truncated_reason == "page_cap_reached"
    assert job.completed == 1000
    assert job.results[0].depth == 0
    assert job.results[0].ok
    assert job.results[1].depth == 1
    assert not job.results[1].ok
    assert job.results[1].error == "target_http_error"


# ── Polymorphic get_job / wait_for_job ────────────────────────────────────────
# get_job and wait_for_job work on either a bulk OR a crawl job_id. The SDK
# uses the response's `kind` field to construct the right typed result.

@respx.mock
def test_get_job_returns_bulk_job_when_kind_is_bulk() -> None:
    """A response with kind='bulk' (or no kind) → BulkJob, single round-trip."""
    job_id = "1c4f9a02-0000-0000-0000-000000000000"
    respx.get(f"{BASE_URL}/bulk/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "bulk",
                "status": "done", "total": 1, "completed": 1,
                "results": [{"url": "https://a.example", "markdown": "## A",
                             "metadata": {"url": "https://a.example"}, "error": None}],
                "created_at": "2026-05-12T14:02:11+00:00",
                "finished_at": "2026-05-12T14:02:14+00:00",
            },
        )
    )

    from wellmarked import BulkJob
    with WellMarked(api_key=API_KEY) as wm:
        job = wm.get_job(job_id)

    assert isinstance(job, BulkJob)
    assert job.kind == "bulk"
    assert job.done


@respx.mock
def test_get_job_redispatches_to_crawl_when_kind_is_crawl() -> None:
    """When /bulk reports kind='crawl', the SDK re-fetches /crawl for the
    proper shape (with depths + truncated fields) and returns a CrawlJob."""
    job_id = "9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    # First call: /bulk/{id} reports the job IS a crawl.
    respx.get(f"{BASE_URL}/bulk/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 1, "completed": 1,
                "results": [],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:00:30+00:00",
            },
        )
    )
    # Second call: /crawl/{id} returns the proper crawl shape.
    respx.get(f"{BASE_URL}/crawl/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 1, "completed": 1,
                "truncated": True, "truncated_reason": "page_cap_reached",
                "results": [
                    {"url": "https://r.example", "depth": 0, "markdown": "## R",
                     "metadata": {"url": "https://r.example"}, "error": None},
                ],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:00:30+00:00",
            },
        )
    )

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.get_job(job_id)

    assert isinstance(job, CrawlJob)
    assert job.kind == "crawl"
    assert job.truncated
    assert job.truncated_reason == "page_cap_reached"
    assert job.results[0].depth == 0


@respx.mock
def test_wait_for_job_uses_typed_endpoint_after_first_call() -> None:
    """wait_for_job's first call goes through get_job (polymorphic). After it
    detects the kind, subsequent polls hit the typed endpoint directly — so
    a crawl job's polling loop doesn't re-pay the /bulk + /crawl dispatch
    on every iteration."""
    job_id = "9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    # Discovery call: /bulk says crawl, /crawl returns processing.
    respx.get(f"{BASE_URL}/bulk/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "processing", "total": 2, "completed": 1,
                "results": [],
                "created_at": "2026-05-15T10:00:00+00:00",
            },
        )
    )
    crawl_route = respx.get(f"{BASE_URL}/crawl/{job_id}")
    crawl_route.side_effect = [
        # First /crawl call: completes the discovery (after /bulk re-dispatch).
        httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "processing", "total": 2, "completed": 1,
                "truncated": False, "truncated_reason": None,
                "results": [],
                "created_at": "2026-05-15T10:00:00+00:00",
            },
        ),
        # Second /crawl call: poll iteration after the sleep — should be DONE.
        httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 2, "completed": 2,
                "truncated": False, "truncated_reason": None,
                "results": [
                    {"url": "https://r.example", "depth": 0, "markdown": "## R",
                     "metadata": {"url": "https://r.example"}, "error": None},
                ],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:01:00+00:00",
            },
        ),
    ]

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.wait_for_job(job_id, poll_interval=0, timeout=5)

    assert isinstance(job, CrawlJob)
    assert job.done
    # /bulk called exactly once (discovery), /crawl called twice (one for
    # discovery re-fetch, one for the actual poll iteration). If we'd
    # routed every poll through get_job we'd see /bulk hit a 2nd time.
    assert respx.calls.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_async_wait_for_job_handles_crawl_jobs() -> None:
    """Same polymorphism on the async client — wait_for_job on a crawl
    job_id returns a CrawlJob without the caller needing a separate
    crawl-specific function."""
    job_id = "9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    respx.get(f"{BASE_URL}/bulk/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 1, "completed": 1,
                "results": [],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:00:30+00:00",
            },
        )
    )
    respx.get(f"{BASE_URL}/crawl/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id, "kind": "crawl",
                "status": "done", "total": 1, "completed": 1,
                "truncated": False, "truncated_reason": None,
                "results": [
                    {"url": "https://r.example", "depth": 0, "markdown": "## R",
                     "metadata": {"url": "https://r.example"}, "error": None},
                ],
                "created_at": "2026-05-15T10:00:00+00:00",
                "finished_at": "2026-05-15T10:00:30+00:00",
            },
        )
    )

    async with AsyncWellMarked(api_key=API_KEY) as wm:
        job = await wm.wait_for_job(job_id, poll_interval=0, timeout=5)

    assert isinstance(job, CrawlJob)
    assert job.done


# ── Custom headers ────────────────────────────────────────────────────────────

@respx.mock
def test_custom_headers_passed_on_every_request() -> None:
    """Caller-supplied headers should ride along on every outbound request."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(
            200,
            json={
                "markdown": "## Hi",
                "metadata": {"url": "https://example.com"},
                "request_id": "44444444-4444-4444-4444-444444444444",
            },
        )

    respx.post(f"{BASE_URL}/extract").mock(side_effect=_capture)

    with WellMarked(
        api_key=API_KEY,
        headers={"X-Trace-Id": "abc123", "X-Tenant": "acme"},
    ) as wm:
        wm.extract("https://example.com")

    assert captured.get("x-trace-id") == "abc123"
    assert captured.get("x-tenant") == "acme"
    # Default auth/content-type still present.
    assert captured.get("authorization") == f"Bearer {API_KEY}"


def test_custom_headers_cannot_overwrite_authorization() -> None:
    """Passing Authorization in `headers=` is a silent no-op — the SDK
    manages auth itself so rotate_key() can still update it."""
    wm = WellMarked(
        api_key=API_KEY,
        headers={"Authorization": "Bearer wm_attacker", "X-Custom": "ok"},
    )
    try:
        assert wm._client.headers["Authorization"] == f"Bearer {API_KEY}"
        assert wm._client.headers["X-Custom"] == "ok"
    finally:
        wm.close()


@respx.mock
def test_set_header_takes_effect_for_subsequent_requests() -> None:
    """set_header() mutates the live client for the rest of the session."""
    captured: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "markdown": "## A",
                "metadata": {"url": "https://example.com"},
                "request_id": "55555555-5555-5555-5555-555555555555",
            },
        )

    respx.post(f"{BASE_URL}/extract").mock(side_effect=_capture)

    with WellMarked(api_key=API_KEY) as wm:
        wm.extract("https://example.com")               # no custom header yet
        wm.set_header("X-Run-Id", "run-99")
        wm.extract("https://example.com")               # now carries it
        wm.remove_header("X-Run-Id")
        wm.extract("https://example.com")               # gone again

    assert "x-run-id" not in captured[0]
    assert captured[1].get("x-run-id") == "run-99"
    assert "x-run-id" not in captured[2]


@pytest.mark.asyncio
@respx.mock
async def test_async_custom_headers_passed_through() -> None:
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(
            200,
            json={
                "markdown": "## Hi",
                "metadata": {"url": "https://example.com"},
                "request_id": "66666666-6666-6666-6666-666666666666",
            },
        )

    respx.post(f"{BASE_URL}/extract").mock(side_effect=_capture)

    async with AsyncWellMarked(
        api_key=API_KEY,
        headers={"X-Trace-Id": "async-trace"},
    ) as wm:
        await wm.extract("https://example.com")

    assert captured.get("x-trace-id") == "async-trace"


# ── Smoke: ExtractResult surfaces only documented attributes ──────────────────

def test_extract_result_attributes_match_api_contract() -> None:
    """ExtractResult should have exactly the three fields the API documents:
    markdown, metadata, request_id — nothing else."""
    result = ExtractResult.from_response(
        {
            "markdown": "x",
            "metadata": {"url": "https://example.com"},
            "request_id": "id",
        }
    )
    # Dataclass fields are the source of truth.
    from dataclasses import fields
    field_names = {f.name for f in fields(result)}
    assert field_names == {"markdown", "metadata", "request_id"}


# ── Idempotency-Key ───────────────────────────────────────────────────────────
# The header is what makes a retried /bulk replay the original job instead of
# double-charging the caller's quota. If the SDK silently stops sending it, the
# API's protection is inert and nothing else would notice.

_QUEUED_JOB = {
    "job_id": "1c4f9a02-0000-0000-0000-000000000000",
    "status": "queued",
    "total": 1,
    "completed": 0,
    "results": [],
}


@respx.mock
def test_bulk_sends_generated_idempotency_key_by_default() -> None:
    route = respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(200, json=_QUEUED_JOB)
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.bulk(["https://a.example"])

    sent = route.calls.last.request.headers.get("Idempotency-Key")
    assert sent, "bulk() must send an Idempotency-Key even when none is passed"


@respx.mock
def test_bulk_honours_an_explicit_idempotency_key() -> None:
    route = respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(200, json=_QUEUED_JOB)
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.bulk(["https://a.example"], idempotency_key="caller-chosen")

    assert route.calls.last.request.headers["Idempotency-Key"] == "caller-chosen"


@respx.mock
def test_each_bulk_call_gets_a_distinct_generated_key() -> None:
    """Two separate submissions are two operations. Reusing one key across them
    would make the second replay the first one's job."""
    route = respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(200, json=_QUEUED_JOB)
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.bulk(["https://a.example"])
        wm.bulk(["https://b.example"])

    first = route.calls[0].request.headers["Idempotency-Key"]
    second = route.calls[1].request.headers["Idempotency-Key"]
    assert first != second


@respx.mock
def test_crawl_sends_idempotency_key() -> None:
    route = respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(
            200,
            json={**_QUEUED_JOB, "kind": "crawl", "total": 0,
                  "truncated": False, "truncated_reason": None},
        )
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.crawl("https://a.example", depth=1)

    assert route.calls.last.request.headers.get("Idempotency-Key")


@respx.mock
def test_per_request_headers_cannot_override_authorization() -> None:
    """The per-request channel must honour RESERVED_HEADERS too — a stray
    Authorization would break rotate_key() mid-session."""
    route = respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(200, json=_QUEUED_JOB)
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm._request(
            "POST", "/bulk", json={"urls": ["https://a.example"]},
            headers={"Authorization": "Bearer attacker", "X-Custom": "kept"},
        )

    sent = route.calls.last.request.headers
    assert sent["Authorization"] == f"Bearer {API_KEY}"
    assert sent["X-Custom"] == "kept"


@respx.mock
async def test_async_bulk_sends_idempotency_key() -> None:
    route = respx.post(f"{BASE_URL}/bulk").mock(
        return_value=httpx.Response(200, json=_QUEUED_JOB)
    )
    async with AsyncWellMarked(api_key=API_KEY) as wm:
        await wm.bulk(["https://a.example"])

    assert route.calls.last.request.headers.get("Idempotency-Key")


# ── Internal retry ────────────────────────────────────────────────────────────
# Retries exist so the auto-generated Idempotency-Key is worth something: a
# connection blip is ambiguous (the job may already exist), and replaying with
# the SAME key collapses the attempts into one job instead of two.


@respx.mock
def test_bulk_retries_connection_error_reusing_the_same_key() -> None:
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) == 1:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json=_QUEUED_JOB)

    respx.post(f"{BASE_URL}/bulk").mock(side_effect=responder)

    with WellMarked(api_key=API_KEY) as wm:
        job = wm.bulk(["https://a.example"])

    assert job.status == "queued"
    assert len(seen) == 2
    # The whole point: both attempts carry the SAME key, so the API replays
    # rather than creating a second job.
    assert seen[0] == seen[1]


@respx.mock
def test_extract_is_never_retried() -> None:
    """A connection error can't tell us whether the extraction happened, and
    /extract takes no Idempotency-Key — so replaying could bill twice."""
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("network down")

    respx.post(f"{BASE_URL}/extract").mock(side_effect=responder)

    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(APIConnectionError):
            wm.extract("https://a.example")

    assert calls["n"] == 1


@respx.mock
def test_bulk_retries_5xx_but_not_4xx() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"code": "x", "message": "down"}})
        return httpx.Response(200, json=_QUEUED_JOB)

    respx.post(f"{BASE_URL}/bulk").mock(side_effect=flaky)
    with WellMarked(api_key=API_KEY) as wm:
        wm.bulk(["https://a.example"])
    assert calls["n"] == 2

    respx.post(f"{BASE_URL}/crawl").mock(
        return_value=httpx.Response(
            422, json={"error": {"code": "crawl_depth_exceeded", "message": "too deep"}}
        )
    )
    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(UnprocessableEntityError):
            wm.crawl("https://a.example", depth=99)
    # 4xx is deterministic — replaying just reproduces it.
    assert respx.routes[-1].call_count == 1


@respx.mock
def test_max_retries_zero_disables_retrying() -> None:
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("network down")

    respx.post(f"{BASE_URL}/bulk").mock(side_effect=down)
    with WellMarked(api_key=API_KEY, max_retries=0) as wm:
        with pytest.raises(APIConnectionError):
            wm.bulk(["https://a.example"])
    assert calls["n"] == 1


@respx.mock
async def test_async_bulk_retries_connection_error() -> None:
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) == 1:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json=_QUEUED_JOB)

    respx.post(f"{BASE_URL}/bulk").mock(side_effect=responder)
    async with AsyncWellMarked(api_key=API_KEY) as wm:
        await wm.bulk(["https://a.example"])

    assert len(seen) == 2 and seen[0] == seen[1]


@respx.mock
def test_client_wide_idempotency_key_does_not_make_extract_retryable() -> None:
    """Regression guard, mirroring the JS SDK.

    Idempotency-Key isn't reserved, so a caller can legally set it client-wide
    via ``set_header``. Replay-safety must still be judged on the PER-REQUEST
    headers only: /extract is billed on arrival and ignores the header, so
    retrying it would double-charge.
    """
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("network down")

    respx.post(f"{BASE_URL}/extract").mock(side_effect=down)

    with WellMarked(api_key=API_KEY) as wm:
        wm.set_header("Idempotency-Key", "smuggled")
        with pytest.raises(APIConnectionError):
            wm.extract("https://a.example")

    assert calls["n"] == 1


@respx.mock
def test_constructor_idempotency_key_does_not_make_extract_retryable() -> None:
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("network down")

    respx.post(f"{BASE_URL}/extract").mock(side_effect=down)

    with WellMarked(api_key=API_KEY, headers={"Idempotency-Key": "smuggled"}) as wm:
        with pytest.raises(APIConnectionError):
            wm.extract("https://a.example")

    assert calls["n"] == 1


# ── Phase 5 continuity: policy overrides, key CRUD, logs ──────────────────────

import json as _json

from wellmarked import ApiKeyInfo, CreatedKey, LogsPage, RevokedKey


@respx.mock
def test_extract_sends_policy_overrides() -> None:
    route = respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(200, json={
            "markdown": "# ok",
            "metadata": {"url": "https://a.example"},
            "request_id": "r1",
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.extract(
            "https://a.example",
            allow_domains=["a.example"],
            deny_patterns=["*/admin/*"],
            respect_robots="strict",
        )
    sent = _json.loads(route.calls.last.request.content)
    assert sent["allow_domains"] == ["a.example"]
    assert sent["deny_patterns"] == ["*/admin/*"]
    assert sent["respect_robots"] == "strict"


@respx.mock
def test_extract_omits_unset_policy_fields() -> None:
    """An override left unset must not appear in the body — otherwise it would
    overwrite the key's own policy with an empty value."""
    route = respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(200, json={
            "markdown": "# ok", "metadata": {"url": "https://a.example"}, "request_id": "r1",
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        wm.extract("https://a.example")
    sent = _json.loads(route.calls.last.request.content)
    assert "allow_domains" not in sent and "deny_patterns" not in sent
    assert "respect_robots" not in sent


@respx.mock
def test_policy_denial_raises_permission_denied() -> None:
    respx.post(f"{BASE_URL}/extract").mock(
        return_value=httpx.Response(403, json={
            "error": {"code": "domain_denied", "message": "denied", "retry": False},
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(PermissionDeniedError) as ei:
            wm.extract("https://blocked.example")
    assert ei.value.code == "domain_denied"


@respx.mock
def test_create_key() -> None:
    route = respx.post(f"{BASE_URL}/keys").mock(
        return_value=httpx.Response(200, json={
            "id": "k1", "api_key": "wm_" + "b" * 40, "name": "ci",
            "scopes": ["extract"], "created_at": "2026-07-17T00:00:00Z",
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        key = wm.create_key(["extract"], name="ci")
    assert isinstance(key, CreatedKey)
    assert key.scopes == ["extract"] and key.api_key.startswith("wm_")
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"scopes": ["extract"], "name": "ci"}


@respx.mock
def test_list_keys() -> None:
    respx.get(f"{BASE_URL}/keys").mock(
        return_value=httpx.Response(200, json={"keys": [
            {"id": "k1", "name": "default", "scopes": ["*"],
             "created_at": "2026-07-01T00:00:00Z", "revoked_at": None},
            {"id": "k2", "name": "ci", "scopes": ["extract"],
             "created_at": "2026-07-02T00:00:00Z", "revoked_at": "2026-07-03T00:00:00Z"},
        ]})
    )
    with WellMarked(api_key=API_KEY) as wm:
        keys = wm.list_keys()
    assert [k.id for k in keys] == ["k1", "k2"]
    assert keys[0].active is True and keys[1].active is False
    assert all(isinstance(k, ApiKeyInfo) for k in keys)


@respx.mock
def test_revoke_key() -> None:
    route = respx.delete(f"{BASE_URL}/keys/k2").mock(
        return_value=httpx.Response(200, json={
            "id": "k2", "revoked_at": "2026-07-03T00:00:00Z",
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        revoked = wm.revoke_key("k2")
    assert isinstance(revoked, RevokedKey) and revoked.id == "k2"
    assert route.calls.last.request.method == "DELETE"


@respx.mock
def test_get_logs_pagination() -> None:
    route = respx.get(f"{BASE_URL}/logs").mock(
        return_value=httpx.Response(200, json={
            "logs": [{
                "id": "r1", "timestamp": "2026-07-17T00:00:00Z",
                "target_url": "https://a.example", "status_code": 403,
                "duration_ms": 3, "error_code": "domain_denied",
                "render_js": False, "key_id": "k1",
                "policy_decision": "domain_denied",
            }],
            "limit": 50, "offset": 0, "has_more": True,
        })
    )
    with WellMarked(api_key=API_KEY) as wm:
        page = wm.get_logs(limit=50, offset=0)
    assert isinstance(page, LogsPage) and page.has_more is True
    assert page.logs[0].policy_decision == "domain_denied"
    assert page.logs[0].key_id == "k1"
    q = str(route.calls.last.request.url)
    assert "limit=50" in q and "offset=0" in q


@respx.mock
@pytest.mark.asyncio
async def test_async_create_key_and_logs() -> None:
    respx.post(f"{BASE_URL}/keys").mock(
        return_value=httpx.Response(200, json={
            "id": "k1", "api_key": "wm_" + "c" * 40, "name": "x",
            "scopes": ["extract"], "created_at": "2026-07-17T00:00:00Z",
        })
    )
    respx.get(f"{BASE_URL}/logs").mock(
        return_value=httpx.Response(200, json={"logs": [], "limit": 50, "offset": 0, "has_more": False})
    )
    async with AsyncWellMarked(api_key=API_KEY) as wm:
        key = await wm.create_key(["extract"], name="x")
        page = await wm.get_logs()
    assert key.id == "k1" and page.has_more is False


# ── Phase 6: self-registration ────────────────────────────────────────────────

from wellmarked import RegisteredAccount


@respx.mock
def test_register_returns_account() -> None:
    route = respx.post(f"{BASE_URL}/register").mock(
        return_value=httpx.Response(200, json={
            "api_key": "wm_" + "d" * 40,
            "user_id": "u1",
            "plan": "free",
            "scopes": ["extract"],
        })
    )
    account = WellMarked.register("agent@example.com", base_url=BASE_URL)
    assert isinstance(account, RegisteredAccount)
    assert account.api_key.startswith("wm_")
    assert account.plan == "free" and account.scopes == ["extract"]
    # No auth header required — register is pre-key.
    sent = route.calls.last.request
    assert "authorization" not in {k.lower() for k in sent.headers.keys()}
    import json as _json
    assert _json.loads(sent.content) == {"email": "agent@example.com"}


@respx.mock
def test_register_rate_limited_raises() -> None:
    respx.post(f"{BASE_URL}/register").mock(
        return_value=httpx.Response(429, json={
            "error": {"code": "register_rate_limited", "message": "slow down", "retry": True},
        })
    )
    with pytest.raises(RateLimitError) as ei:
        WellMarked.register("agent@example.com", base_url=BASE_URL)
    assert ei.value.code == "register_rate_limited"


@respx.mock
@pytest.mark.asyncio
async def test_async_register() -> None:
    respx.post(f"{BASE_URL}/register").mock(
        return_value=httpx.Response(200, json={
            "api_key": "wm_" + "e" * 40, "user_id": "u2",
            "plan": "free", "scopes": ["extract"],
        })
    )
    account = await AsyncWellMarked.register("agent@example.com", base_url=BASE_URL)
    assert account.user_id == "u2" and account.scopes == ["extract"]


# ── Search ──────────────────────────────────────────────────────────────────────

_SEARCH_BODY = {
    "query": "python asyncio",
    "results": [
        {"url": "https://a.test/1", "status": "ok", "title": "A", "snippet": "s1", "markdown": "# A"},
        {"url": "https://b.test/2", "status": "error", "title": "B", "snippet": "s2", "error": "target_timeout"},
    ],
    "request_id": "33333333-3333-3333-3333-333333333333",
}


@respx.mock
def test_search_success() -> None:
    route = respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json=_SEARCH_BODY)
    )

    with WellMarked(api_key=API_KEY) as wm:
        res = wm.search("python asyncio", num_results=2)

    # Request shape reached the server unchanged.
    import json as _json
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"query": "python asyncio", "num_results": 2, "render_js": False}

    assert isinstance(res, SearchResults) and res.query == "python asyncio"
    assert res.request_id == "33333333-3333-3333-3333-333333333333"
    assert len(res.results) == 2
    ok, err = res.results
    assert isinstance(ok, SearchResult) and ok.ok and ok.markdown == "# A" and ok.title == "A"
    # A failed page still carries the provider snippet + a stable error code.
    assert not err.ok and err.error == "target_timeout" and err.snippet == "s2"


@respx.mock
def test_search_plan_gate_raises() -> None:
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "plan_not_supported", "message": "Pro+ only."}}
        )
    )
    with WellMarked(api_key=API_KEY) as wm:
        with pytest.raises(PermissionDeniedError) as ei:
            wm.search("q")
    assert ei.value.code == "plan_not_supported"


@pytest.mark.asyncio
@respx.mock
async def test_async_search_success() -> None:
    respx.post(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(200, json=_SEARCH_BODY)
    )
    async with AsyncWellMarked(api_key=API_KEY) as wm:
        res = await wm.search("python asyncio")
    assert isinstance(res, SearchResults) and len(res.results) == 2
    assert res.results[0].ok and res.results[1].error == "target_timeout"
