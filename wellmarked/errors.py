"""Exception hierarchy for the WellMarked SDK.

Every HTTP error returned by the API is translated into a typed exception
whose class corresponds to the HTTP status and whose ``code`` matches the
``error.code`` field in the response body. Catch ``WellMarkedError`` for
anything raised by the SDK; catch a more specific subclass when you want
to handle one failure mode.
"""
from __future__ import annotations

from typing import Optional


class WellMarkedError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        retry_after: Optional[int] = None,
        retry_after_ms: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        # Whole-second back-off (HTTP-standard Retry-After). Set on every
        # 429 the API returns — monthly quota (seconds until period reset)
        # and per-second cap (rounded up from the millisecond hint).
        self.retry_after = retry_after
        # Sub-second back-off, only populated for ``rate_limit_too_fast``
        # 429s. Lets callers retry as soon as the per-tier slot frees
        # without sleeping a full second. None when the server didn't
        # send a Retry-After-Ms header.
        self.retry_after_ms = retry_after_ms
        self.request_id = request_id

    def __repr__(self) -> str:
        parts = [f"message={self.message!r}"]
        if self.code is not None:
            parts.append(f"code={self.code!r}")
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        return f"{self.__class__.__name__}({', '.join(parts)})"


class APIConnectionError(WellMarkedError):
    """Raised when the SDK couldn't reach the API (DNS, TCP, TLS, timeout)."""


class APIStatusError(WellMarkedError):
    """Raised for any non-2xx response from the API."""


class AuthenticationError(APIStatusError):
    """401 — missing or invalid API key."""


class PermissionDeniedError(APIStatusError):
    """403 — account inactive, plan does not allow this operation, or job belongs to another user."""


class NotFoundError(APIStatusError):
    """404 — job not found or expired past the 6-hour retention window."""


class UnprocessableEntityError(APIStatusError):
    """422 — request was syntactically valid but couldn't be fulfilled.

    Common ``code`` values:

    - ``no_content``            — could not identify main content on the page
    - ``target_timeout``        — the target URL timed out
    - ``bulk_cap_exceeded``     — more URLs than the plan allows per request
    - ``crawl_depth_exceeded``  — requested depth above the plan cap
    """


class RateLimitError(APIStatusError):
    """429 — a request was rejected for exceeding a rate limit.

    The ``code`` attribute discriminates which limit was hit:

    - ``rate_limit_exceeded`` — the monthly plan quota is exhausted.
      ``retry_after`` is the number of **seconds** until the period
      resets (often hours or days). ``retry_after_ms`` is ``None``.
    - ``rate_limit_too_fast`` — the per-second cap was hit (Free 5/s,
      Pro 20/s, Growth 100/s, Enterprise unlimited). ``retry_after_ms``
      is the precise millisecond back-off; ``retry_after`` is the same
      value rounded up to a whole second for clients that only respect
      the HTTP-standard header.

    Branch on ``code`` to choose between sleeping milliseconds and
    waiting for the next billing period.
    """


class InternalServerError(APIStatusError):
    """5xx — something went wrong on the API side."""


# Status → exception class lookup used by the client to translate responses.
_STATUS_TO_EXC: dict[int, type[APIStatusError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def from_response(
    status_code: int,
    body: object,
    *,
    request_id: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> APIStatusError:
    """Build the right exception subclass for a given HTTP status + JSON body.

    ``headers`` is optional — when supplied, the function reads
    ``Retry-After-Ms`` from it so the resulting :class:`RateLimitError`
    can expose millisecond-precise back-off on the per-second
    (``rate_limit_too_fast``) variant.
    """
    code: Optional[str] = None
    message: str = f"HTTP {status_code}"
    retry_after: Optional[int] = None
    retry_after_ms: Optional[int] = None

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            raw_code = err.get("code")
            raw_msg = err.get("message")
            raw_retry = err.get("retry_after")
            if isinstance(raw_code, str):
                code = raw_code
            if isinstance(raw_msg, str):
                message = raw_msg
            # `bool` is a subclass of `int` in Python, so `isinstance(True, int)`
            # returns True. Explicitly exclude bools — otherwise a future
            # `retry_after: true` in the response body would become
            # `retry_after=1` and clients would sleep one second for a
            # totally unrelated condition.
            if isinstance(raw_retry, int) and not isinstance(raw_retry, bool):
                retry_after = raw_retry

    if headers:
        # Header lookups are case-insensitive in HTTP but the dict we
        # receive may or may not normalize. Try both common shapes.
        ms_raw = headers.get("Retry-After-Ms") or headers.get("retry-after-ms")
        if isinstance(ms_raw, str) and ms_raw.isdigit():
            retry_after_ms = int(ms_raw)

    exc_cls: type[APIStatusError]
    if status_code >= 500:
        exc_cls = InternalServerError
    else:
        exc_cls = _STATUS_TO_EXC.get(status_code, APIStatusError)

    return exc_cls(
        message,
        code=code,
        status_code=status_code,
        retry_after=retry_after,
        retry_after_ms=retry_after_ms,
        request_id=request_id,
    )
