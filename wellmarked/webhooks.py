"""WellMarked webhook signature verification.

Use this when your service receives a ``job.completed`` delivery from
WellMarked at the ``webhook_url`` you supplied to :meth:`bulk` /
:meth:`crawl`. The function rejects bad signatures and stale deliveries
and returns the parsed JSON payload on success.

    from fastapi import Request
    from wellmarked import verify_webhook, WebhookVerificationError

    @app.post("/hooks/wm")
    async def hook(request: Request):
        try:
            payload = verify_webhook(
                secret=WELLMARKED_WEBHOOK_SECRET,    # whsec_... — store env-side
                headers=request.headers,
                body=await request.body(),
            )
        except WebhookVerificationError:
            return Response(status_code=401)
        # payload is the validated job.completed dict
        ...

The signing scheme:

* ``key      = bytes.fromhex(secret.removeprefix("whsec_"))``
* ``message  = f"{delivery_id}.{timestamp}.{body}".encode()``  (body bytes appended raw)
* ``digest   = HMAC-SHA256(key, message)``
* ``header   = "v1," + base64(digest)``

Deliveries older than ``max_age_sec`` (default 300s = 5 min) are
rejected to defeat replay attacks — WellMarked retries with fresh
timestamps so this never affects a legitimate delivery.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping


_DEFAULT_MAX_AGE_SEC = 300
_SECRET_PREFIX = "whsec_"


class WebhookVerificationError(Exception):
    """Raised by :func:`verify_webhook` on any verification failure.

    Callers should map this to HTTP 401. The error message identifies the
    failure mode for *your* logs — do NOT echo it back to the request
    sender; an attacker probing for the right shape should get the same
    response for every failure type.
    """


def verify_webhook(
    *,
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    max_age_sec: int = _DEFAULT_MAX_AGE_SEC,
) -> dict[str, Any]:
    """Verify a WellMarked webhook delivery. Returns the parsed payload.

    Args:
        secret: The signing secret WellMarked sends with deliveries.
            Format ``whsec_<64 hex chars>``. Store env-side, never
            check into source control.
        headers: Incoming request headers. Lookup is case-insensitive,
            so most framework header objects work directly (Starlette,
            Flask, Django, dict).
        body: The raw request body bytes — NOT a decoded string. The
            signature is computed over the raw bytes the server signed;
            UTF-8 round-tripping a payload changes the signature on
            non-ASCII inputs.
        max_age_sec: Reject deliveries older than this. Defaults to 300
            (5 minutes). Set higher only if your endpoint has clock
            skew greater than 5 minutes against UTC.

    Returns:
        The parsed JSON payload (the same dict WellMarked sent).

    Raises:
        WebhookVerificationError: signature mismatch, stale timestamp,
            missing headers, bad secret encoding, or unparseable body.
    """
    if not secret:
        raise WebhookVerificationError("Webhook secret not configured.")

    # Body must be raw bytes. A common mistake is to pass
    # request.get_data().decode() or request.json() — neither matches
    # what we signed, so the signature would never verify. Catching the
    # type mismatch up front yields a clearer error than the cryptic
    # TypeError that would come out of the byte concatenation below.
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise WebhookVerificationError(
            f"Webhook body must be raw bytes, got {type(body).__name__}. "
            "Pass request.body() (FastAPI) / request.get_data() (Flask) "
            "/ request.body (Django) — NOT a decoded string or parsed JSON."
        )
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)

    def _h(name: str) -> str:
        # Case-insensitive header pull. Most framework header maps already
        # do this, but the fallback iteration covers plain dicts.
        for k in (name, name.lower(), name.upper()):
            v = headers.get(k)
            if v:
                return v
        for k, v in headers.items():
            if k.lower() == name.lower():
                return v
        return ""

    delivery_id = _h("x-wellmarked-delivery-id")
    timestamp   = _h("x-wellmarked-timestamp")
    signature   = _h("x-wellmarked-signature")
    if not (delivery_id and timestamp and signature):
        raise WebhookVerificationError("Missing webhook signature headers.")

    try:
        ts_int = int(timestamp)
    except ValueError:
        raise WebhookVerificationError("Bad X-WellMarked-Timestamp.")
    if abs(time.time() - ts_int) > max_age_sec:
        raise WebhookVerificationError("Stale webhook delivery.")

    try:
        key = bytes.fromhex(secret.removeprefix(_SECRET_PREFIX))
    except ValueError:
        raise WebhookVerificationError("Webhook secret is not valid hex.")

    # Encode the id+timestamp prefix as ASCII. A tampered request whose
    # headers contain non-ASCII bytes would otherwise raise
    # UnicodeEncodeError out of this function — not the WebhookVerificationError
    # the caller's handler is set up to catch.
    try:
        prefix = delivery_id.encode("ascii") + b"." + timestamp.encode("ascii") + b"."
    except UnicodeEncodeError:
        raise WebhookVerificationError(
            "Webhook signature headers contain non-ASCII characters."
        )
    msg = prefix + body
    expected = base64.b64encode(
        hmac.new(key, msg, hashlib.sha256).digest()
    ).decode("ascii")

    # The header carries one or more space-separated entries of the form
    # ``v1,<base64>``. Multiple entries support secret-rotation overlap
    # (mirrors Svix and Stripe). Any one matching is sufficient.
    matched = False
    for entry in signature.split():
        if "," not in entry:
            continue
        version, value = entry.split(",", 1)
        if version != "v1":
            continue
        if hmac.compare_digest(value, expected):
            matched = True
            break

    if not matched:
        raise WebhookVerificationError("Webhook signature mismatch.")

    try:
        return json.loads(body)
    except (ValueError, TypeError) as e:
        raise WebhookVerificationError(f"Webhook body is not valid JSON: {e}")
