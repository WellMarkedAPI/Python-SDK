"""Typed response objects returned by the WellMarked SDK.

These mirror the JSON shapes documented at https://api.wellmarked.io/docs
but are plain ``dataclasses`` so the SDK has no Pydantic dependency.

Response objects carry only the body fields documented in the API
reference — they do not surface HTTP headers. Quota state lives on the
account, so use :meth:`wellmarked.WellMarked.get_usage` to read it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp returned by the API.

    The API emits ``...Z`` suffixes; ``datetime.fromisoformat`` in 3.11+ handles
    them, but on 3.9/3.10 we have to translate to ``+00:00`` first.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── Extraction ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtractionMeta:
    """Per-article metadata returned with each extraction.

    ``date`` is the article's published date as a string (often ``None`` —
    not every page publishes one). ``retrieved_at`` is the timestamp at
    which WellMarked actually fetched the page, populated on every
    successful extraction; the two fields are independent.
    """
    url: str
    title: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    retrieved_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExtractionMeta":
        return cls(
            url=str(data.get("url", "")),
            title=data.get("title"),
            author=data.get("author"),
            date=data.get("date"),
            # Older API responses (or fixtures captured before this field
            # existed) won't carry retrieved_at — _parse_dt returns None
            # for missing/unparseable values so the dataclass stays valid.
            retrieved_at=_parse_dt(data.get("retrieved_at")),
        )


@dataclass(frozen=True)
class ExtractResult:
    """Result of ``POST /extract``."""
    markdown: str
    metadata: ExtractionMeta
    request_id: str

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "ExtractResult":
        return cls(
            markdown=str(body.get("markdown", "")),
            metadata=ExtractionMeta.from_dict(body.get("metadata", dict())),
            request_id=str(body.get("request_id", "")),
        )


# ── Search ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SearchResult:
    """One result in a :class:`SearchResults`.

    On success ``status == "ok"`` and ``markdown`` is populated; on a per-page
    failure ``status == "error"`` and ``error`` carries a stable code (same
    convention as :class:`BulkItem.error`). ``title`` is the extracted title on
    success, else the search provider's; ``snippet`` is always the provider's
    result snippet, so a page that failed extraction still carries context.
    """
    url: str
    status: str            # "ok" | "error"
    title: Optional[str] = None
    snippet: Optional[str] = None
    markdown: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchResult":
        return cls(
            url=str(data.get("url", "")),
            status=str(data.get("status", "error")),
            title=data.get("title"),
            snippet=data.get("snippet"),
            markdown=data.get("markdown"),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class SearchResults:
    """Result of ``POST /search`` — the query plus the extracted result pages.

    Synchronous: unlike bulk/crawl there is no job to poll; ``results`` is
    already populated (with partial failures marked per item).
    """
    query: str
    results: List[SearchResult]
    request_id: str

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "SearchResults":
        raw = body.get("results") or []
        return cls(
            query=str(body.get("query", "")),
            results=[SearchResult.from_dict(r) for r in raw],
            request_id=str(body.get("request_id", "")),
        )


# ── Bulk ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BulkItem:
    """One entry in a bulk job's ``results`` list.

    On success, ``markdown`` and ``metadata`` are populated and ``error`` is
    ``None``. On a per-URL failure, ``markdown``/``metadata`` are ``None`` and
    ``error`` carries a stable API error **code** — never a human message — e.g.
    ``target_timeout``, ``domain_denied``, ``internal_error``. Same convention
    as :class:`CrawlItem.error`, so results parse identically on either endpoint.
    """
    url: str
    markdown: Optional[str] = None
    metadata: Optional[ExtractionMeta] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.markdown is not None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BulkItem":
        raw_meta = data.get("metadata")
        return cls(
            url=str(data.get("url", "")),
            markdown=data.get("markdown"),
            metadata=ExtractionMeta.from_dict(raw_meta) if raw_meta is not None else None,
            error=data.get("error"),
        )


@dataclass(frozen=True)
class BulkJob:
    """Status of a bulk extraction job.

    Returned from both ``POST /bulk`` (initial submission) and
    ``GET /bulk/{job_id}`` (status polling). Also one of the two
    possible return types from the polymorphic
    :meth:`wellmarked.WellMarked.get_job` and
    :meth:`wellmarked.WellMarked.wait_for_job` —
    use ``isinstance(job, BulkJob)`` (or check ``job.kind == "bulk"``)
    to distinguish from :class:`CrawlJob` if you need to read crawl-
    specific fields.
    """
    job_id: str
    status: str            # "queued" | "processing" | "done"
    total: int
    completed: int
    results: List[BulkItem]
    # Discriminator field. Defaults to "bulk" — populated from the API
    # response. The polymorphic get_job uses this to construct the right
    # dataclass (BulkJob vs CrawlJob).
    kind: str = "bulk"
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Populated ONLY on the submission that first minted this account's
    # signing secret (one-time visibility, Stripe-style). Always None on
    # poll responses. Save it on receipt and pass it to
    # :func:`wellmarked.verify_webhook`; if you lose it, call
    # :meth:`wellmarked.WellMarked.rotate_webhook_secret`.
    webhook_signing_secret: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.status == "done"

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "BulkJob":
        raw_results = body.get("results") or []
        return cls(
            job_id=str(body.get("job_id", "")),
            kind=str(body.get("kind", "bulk")),
            status=str(body.get("status", "queued")),
            total=int(body.get("total", 0)),
            completed=int(body.get("completed", 0)),
            results=[BulkItem.from_dict(r) for r in raw_results],
            created_at=_parse_dt(body.get("created_at")),
            finished_at=_parse_dt(body.get("finished_at")),
            webhook_signing_secret=body.get("webhook_signing_secret"),
        )


# ── Crawl ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CrawlItem:
    """One page in a crawl job's ``results`` list.

    Shape mirrors :class:`BulkItem` with an added ``depth`` field showing how
    far from the root URL this page sits in the BFS.
    """
    url: str
    depth: int = 0
    markdown: Optional[str] = None
    metadata: Optional[ExtractionMeta] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.markdown is not None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CrawlItem":
        raw_meta = data.get("metadata")
        return cls(
            url=str(data.get("url", "")),
            depth=int(data.get("depth", 0)),
            markdown=data.get("markdown"),
            metadata=ExtractionMeta.from_dict(raw_meta) if raw_meta is not None else None,
            error=data.get("error"),
        )


@dataclass(frozen=True)
class CrawlJob:
    """Status of a crawl job. Returned from ``POST /crawl`` and ``GET /crawl/{job_id}``.

    Also one of the two possible return types from the polymorphic
    :meth:`wellmarked.WellMarked.get_job` and
    :meth:`wellmarked.WellMarked.wait_for_job`.

    Two crawl-only fields:
      * ``truncated`` — True when the crawl stopped before exhausting the
        frontier (depth/page cap hit, or per-page metering ran out of quota).
      * ``truncated_reason`` — ``"page_cap_reached"`` | ``"quota_exhausted"`` |
        ``None``.
    """
    job_id: str
    status: str            # "queued" | "processing" | "done"
    total: int
    completed: int
    results: List[CrawlItem]
    truncated: bool = False
    truncated_reason: Optional[str] = None
    # Discriminator field. Defaults to "crawl" — see BulkJob.kind.
    kind: str = "crawl"
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # See :attr:`BulkJob.webhook_signing_secret` — same semantics here.
    webhook_signing_secret: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.status == "done"

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "CrawlJob":
        raw_results = body.get("results") or []
        return cls(
            job_id=str(body.get("job_id", "")),
            kind=str(body.get("kind", "crawl")),
            status=str(body.get("status", "queued")),
            total=int(body.get("total", 0)),
            completed=int(body.get("completed", 0)),
            results=[CrawlItem.from_dict(r) for r in raw_results],
            truncated=bool(body.get("truncated", False)),
            truncated_reason=body.get("truncated_reason"),
            created_at=_parse_dt(body.get("created_at")),
            finished_at=_parse_dt(body.get("finished_at")),
            webhook_signing_secret=body.get("webhook_signing_secret"),
        )


# ── Usage ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Usage:
    """Result of ``GET /usage`` — current-period quota state.

    This is the source of truth for rate-limit / quota information. The SDK
    does not surface ``X-RateLimit-*`` headers on extract/bulk responses;
    call :meth:`wellmarked.WellMarked.get_usage` instead.
    """
    plan: str
    period: str            # e.g. "2026-05"
    used: int
    limit: int
    remaining: int

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "Usage":
        return cls(
            plan=str(body.get("plan", "")),
            period=str(body.get("period", "")),
            used=int(body.get("used", 0)),
            limit=int(body.get("limit", 0)),
            remaining=int(body.get("remaining", 0)),
        )


# ── Key rotation ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RotatedKey:
    """Result of ``POST /keys/rotate``.

    ``api_key`` is the new raw key — store it before discarding this object,
    there is no recovery flow. The previous key is invalidated the moment
    the rotation call returns 200.
    """
    api_key: str
    rotated_at: Optional[datetime] = None

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "RotatedKey":
        return cls(
            api_key=str(body.get("api_key", "")),
            rotated_at=_parse_dt(body.get("rotated_at")),
        )


# ── Self-registration ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegisteredAccount:
    """Result of :meth:`wellmarked.WellMarked.register` (``POST /register``).

    ``api_key`` is the new raw key — shown once, store it. The account is
    deliberately weak: ``plan="free"`` and ``scopes=["extract"]``. Build a
    client with it via ``WellMarked(api_key=account.api_key)``.
    """
    api_key: str
    user_id: str
    plan: str
    scopes: List[str]

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "RegisteredAccount":
        return cls(
            api_key=str(body.get("api_key", "")),
            user_id=str(body.get("user_id", "")),
            plan=str(body.get("plan", "")),
            scopes=list(body.get("scopes") or []),
        )


# ── Key management (scoped keys) ──────────────────────────────────────────────

@dataclass(frozen=True)
class CreatedKey:
    """Result of ``create_key`` (``POST /keys``).

    ``api_key`` is the new raw key — shown once, store it before discarding this
    object. ``scopes`` is the subset it was granted (extract / bulk / crawl /
    keys).
    """
    id: str
    api_key: str
    name: str
    scopes: List[str]
    created_at: Optional[datetime] = None

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "CreatedKey":
        return cls(
            id=str(body.get("id", "")),
            api_key=str(body.get("api_key", "")),
            name=str(body.get("name", "")),
            scopes=list(body.get("scopes") or []),
            created_at=_parse_dt(body.get("created_at")),
        )


@dataclass(frozen=True)
class ApiKeyInfo:
    """One key's metadata from ``list_keys`` (``GET /keys``). Never carries the
    raw key. ``revoked_at`` is set once the key has been revoked."""
    id: str
    name: str
    scopes: List[str]
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiKeyInfo":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            scopes=list(data.get("scopes") or []),
            created_at=_parse_dt(data.get("created_at")),
            revoked_at=_parse_dt(data.get("revoked_at")),
        )


@dataclass(frozen=True)
class RevokedKey:
    """Result of ``revoke_key`` (``DELETE /keys/{id}``)."""
    id: str
    revoked_at: Optional[datetime] = None

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "RevokedKey":
        return cls(
            id=str(body.get("id", "")),
            revoked_at=_parse_dt(body.get("revoked_at")),
        )


# ── Audit log ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LogEntry:
    """One row of your request history from ``get_logs`` (``GET /logs``).

    ``policy_decision`` records how the key's compliance policy decided:
    ``"allowed"`` | ``"domain_not_allowed"`` | ``"domain_denied"`` |
    ``"robots_disallowed"``. ``key_id`` attributes the request to a key.
    """
    id: str
    target_url: str
    status_code: int
    duration_ms: int
    timestamp: Optional[datetime] = None
    error_code: Optional[str] = None
    render_js: Optional[bool] = None
    key_id: Optional[str] = None
    policy_decision: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LogEntry":
        return cls(
            id=str(data.get("id", "")),
            target_url=str(data.get("target_url", "")),
            status_code=int(data.get("status_code", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            timestamp=_parse_dt(data.get("timestamp")),
            error_code=data.get("error_code"),
            render_js=data.get("render_js"),
            key_id=data.get("key_id"),
            policy_decision=data.get("policy_decision"),
        )


@dataclass(frozen=True)
class LogsPage:
    """One page of ``get_logs`` results. ``has_more`` is True when further rows
    exist beyond this page — advance by ``offset += limit``."""
    logs: List[LogEntry]
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "LogsPage":
        return cls(
            logs=[LogEntry.from_dict(r) for r in (body.get("logs") or [])],
            limit=int(body.get("limit", 0)),
            offset=int(body.get("offset", 0)),
            has_more=bool(body.get("has_more", False)),
        )


# ── Webhook secret rotation ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RotatedWebhookSecret:
    """Result of ``POST /webhook/rotate``.

    ``webhook_signing_secret`` is the new raw secret — store it before
    discarding this object, there is no recovery flow other than
    rotating again. The previous secret is invalidated the moment the
    rotation call returns 200, and any deliveries already in the retry
    queue will be signed with the NEW secret on their next attempt.
    """
    webhook_signing_secret: str
    rotated_at: Optional[datetime] = None

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "RotatedWebhookSecret":
        return cls(
            webhook_signing_secret=str(body.get("webhook_signing_secret", "")),
            rotated_at=_parse_dt(body.get("rotated_at")),
        )
