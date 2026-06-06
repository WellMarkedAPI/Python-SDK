"""Shared configuration and helpers for the sync and async clients."""
from __future__ import annotations

import os
from typing import Any, Optional

from ._version import __version__
from .errors import APIConnectionError, APIStatusError, from_response

DEFAULT_BASE_URL = "https://api.wellmarked.io"
DEFAULT_TIMEOUT = 30.0


def resolve_api_key(api_key: Optional[str]) -> str:
    """Take an explicit key or fall back to ``WELLMARKED_API_KEY``."""
    key = api_key or os.environ.get("WELLMARKED_API_KEY")
    if not key:
        raise ValueError(
            "No API key provided. Pass api_key=... to the client or set the "
            "WELLMARKED_API_KEY environment variable. Generate a key at "
            "https://wellmarked.io."
        )
    return key


def default_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"wellmarked-python/{__version__}",
    }


# Headers the SDK manages itself and won't allow callers to overwrite. Without
# this guard, a stray `headers={"Authorization": "..."}` could silently
# replace the bearer token and rotate_key() would stop working mid-session.
_RESERVED_HEADERS = {"authorization", "content-type", "accept"}


def merge_headers(
    api_key: str,
    extra: Optional[dict[str, str]],
) -> dict[str, str]:
    """Combine our default headers with any caller-supplied extras.

    Reserved headers (Authorization, Content-Type, Accept) are always taken
    from the defaults — passing them in ``extra`` is a silent no-op rather
    than an error so a caller doing ``headers={"X-Custom": ..., "Accept": ...}``
    doesn't have to special-case anything.
    """
    out = default_headers(api_key)
    if not extra:
        return out
    for k, v in extra.items():
        if k.lower() in _RESERVED_HEADERS:
            continue
        out[k] = v
    return out


def parse_response(
    status_code: int,
    body: Any,
    *,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    """Return the response body, or raise the appropriate ``APIStatusError``.

    Quota state (X-RateLimit-Limit / -Remaining / -Reset) is intentionally
    not surfaced on success — that belongs on the account, accessible via
    ``GET /usage``. On error, headers ARE inspected: ``Retry-After-Ms``
    populates :attr:`RateLimitError.retry_after_ms` for the per-second
    ``rate_limit_too_fast`` 429 so callers can back off precisely.
    """
    request_id: Optional[str] = None
    if isinstance(body, dict):
        rid = body.get("request_id")
        if isinstance(rid, str):
            request_id = rid

    if 200 <= status_code < 300:
        if body is None:
            # The API contract says every documented endpoint returns a JSON
            # body on 2xx. None here means the server broke that contract
            # (or a middlebox stripped the body); fail loudly with a clear
            # error rather than letting downstream parsing raise
            # AttributeError on None.get(...). APIStatusError is the right
            # class — there IS a status code, so `except APIStatusError`
            # handlers catch this alongside every other server-side
            # failure mode without needing a separate branch.
            raise APIStatusError(
                f"API returned HTTP {status_code} with no JSON body. "
                "This is a contract violation — please report it.",
                status_code=status_code,
            )
        return body

    raise from_response(status_code, body, request_id=request_id, headers=headers)


def wrap_transport_error(exc: BaseException) -> APIConnectionError:
    """Wrap an httpx transport-level error in a stable SDK exception."""
    return APIConnectionError(f"Could not reach the WellMarked API: {exc!r}")
