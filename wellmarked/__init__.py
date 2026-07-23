"""Official Python SDK for the WellMarked API.

    from wellmarked import WellMarked

    with WellMarked(api_key="wm_...") as wm:
        result = wm.extract("https://example.com/article")
        print(result.markdown)

See https://wellmarked.io/docs for the full API reference.
"""
from ._version import __version__
from .async_client import AsyncWellMarked
from .client import WellMarked
from .errors import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
    WellMarkedError,
)
from .models import (
    ApiKeyInfo,
    BulkItem,
    Chunk,
    ContentBlock,
    ContentMetrics,
    BulkJob,
    CrawlItem,
    CrawlJob,
    CreatedKey,
    ExtractionMeta,
    ExtractResult,
    LogEntry,
    LogsPage,
    RegisteredAccount,
    RevokedKey,
    RotatedKey,
    RotatedWebhookSecret,
    SearchResult,
    SearchResults,
    Usage,
)
from .webhooks import WebhookPayload, WebhookVerificationError, verify_webhook

__all__ = [
    "__version__",
    # Clients
    "WellMarked",
    "AsyncWellMarked",
    # Models
    "ApiKeyInfo",
    "BulkItem",
    "Chunk",
    "ContentBlock",
    "ContentMetrics",
    "BulkJob",
    "CrawlItem",
    "CrawlJob",
    "CreatedKey",
    "ExtractionMeta",
    "ExtractResult",
    "LogEntry",
    "LogsPage",
    "RegisteredAccount",
    "RevokedKey",
    "RotatedKey",
    "RotatedWebhookSecret",
    "SearchResult",
    "SearchResults",
    "Usage",
    # Errors
    "WellMarkedError",
    "APIConnectionError",
    "APIStatusError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    # Webhooks
    "verify_webhook",
    "WebhookVerificationError",
    "WebhookPayload",
]
