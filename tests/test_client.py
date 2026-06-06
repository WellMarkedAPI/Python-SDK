"""Mocked-transport tests for the sync and async clients."""
from __future__ import annotations

import httpx
import pytest
import respx

from wellmarked import (
    AsyncWellMarked,
    AuthenticationError,
    BulkItem,
    CrawlJob,
    ExtractionMeta,
    ExtractResult,
    PermissionDeniedError,
    RateLimitError,
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
