# wellmarked

[![PyPI](https://img.shields.io/pypi/v/wellmarked.svg)](https://pypi.org/project/wellmarked/)
[![Python](https://img.shields.io/pypi/pyversions/wellmarked.svg)](https://pypi.org/project/wellmarked/)

Official Python SDK for the **[WellMarked](https://wellmarked.io)** API — convert any URL to clean Markdown.

```bash
pip install wellmarked
```

## Quick start

```python
from wellmarked import WellMarked

with WellMarked(api_key="wm_...") as wm:
    result = wm.extract("https://example.com/article")
    print(result.markdown)
    print(result.metadata.title, "by", result.metadata.author)
    print("retrieved at", result.metadata.retrieved_at)
```

`result.metadata.retrieved_at` is a `datetime` (UTC) recording when WellMarked actually fetched the page — distinct from `result.metadata.date` (the article's published date, often `None`). Useful for cache-freshness checks on the caller's side.

The API key can also be picked up from the `WELLMARKED_API_KEY` environment variable, in which case `WellMarked()` is enough.

Get a key at [wellmarked.io](https://wellmarked.io) — or [self-register](#self-registration) with an email.

## Pricing

|                       | Free      | Pro                  | Growth               | Enterprise    |
|-----------------------|-----------|----------------------|----------------------|---------------|
| **Monthly Price**     | $0        | $29/mo               | $79/mo               | $199/mo       |
| **Included Requests** | 1,000/mo  | 10,000/mo            | 40,000/mo            | 250,000/mo    |
| **Annual Price**      | —         | $299/yr              | $799/yr              | $1,999/yr     |
| **Bulk Requests**     | ✅ (up to 5/request) | ✅ (up to 50/request) | ✅ (up to 200/request) | ✅ (Unlimited) |
| **Search**            | ❌         | ✅                    | ✅                    | ✅             |
| **Crawl**             | ❌         | ✅ (2,000 pages/job)  | ✅ (10,000 pages/job) | ✅ (Unlimited) |
| **`json`/`chunks` formats** | ❌  | ✅                    | ✅                    | ✅             |
| **Overage Rate**      | —         | $0.0035/req          | $0.0020/req          | $0.0012/req   |
| **JS Rendering**      | ❌         | ✅                    | ✅                    | ✅             |
| **Priority Queue**    | Last      | Standard             | High                 | Highest       |

See additional pricing information at [wellmarked.io/#pricing](https://wellmarked.io/#pricing).

## Async

`AsyncWellMarked` is a drop-in async equivalent — every endpoint method is a coroutine.

```python
import asyncio
from wellmarked import AsyncWellMarked

async def main():
    async with AsyncWellMarked() as wm:
        result = await wm.extract("https://example.com/article")
        print(result.markdown)

asyncio.run(main())
```

## Output formats

`extract`, `search`, `bulk`, and `crawl` all take a `format` argument. Exactly one content field is populated per result; the rest stay `None`. `.content` returns whichever one came back.

```python
r = wm.extract("https://example.com/article", format="chunks")
for chunk in r.chunks:                       # ready-to-embed token windows
    print(chunk.start_token, chunk.end_token, chunk.text[:40])

wm.extract(url, format="json").blocks        # typed heading/paragraph/list/code blocks
wm.extract(url, format="html").html          # cleaned main-content HTML
wm.extract(url, format="links").links        # every http(s) link in the content
wm.extract(url).content                      # whichever field the format populated
```

| `format`     | Field      | Plan  |
|--------------|------------|-------|
| `"markdown"` | `.markdown`| All   |
| `"html"`     | `.html`    | All   |
| `"links"`    | `.links`   | All   |
| `"json"`     | `.blocks`  | Pro+  |
| `"chunks"`   | `.chunks`  | Pro+  |

Every extraction result also carries `.metrics` (`content_bytes`, `input_tokens`, `output_tokens`, `tokens_saved`, `reduction_pct`) — the token-savings ROI of the extraction, or `None` when the tokenizer is unavailable.

## Search

Search the web and extract every result in one synchronous call (Pro plan and above). Costs `1 + len(results)` requests. Results come back partial — a slow or blocked page is an error item, never sinks the call.

```python
results = wm.search("retrieval augmented generation", num_results=5)
for hit in results.results:
    if hit.ok:
        print(hit.title, "→", hit.markdown[:80])
    else:
        print(f"{hit.url} failed: {hit.error}  (snippet: {hit.snippet})")
```

`num_results` is clamped to 1..10 server-side. `search` takes the full extraction parameter set — `format` and the [compliance overrides](#compliance-overrides) apply to every result. (It does not take `retry`; its per-result deadline can't absorb one.)

## Bulk extraction

Submit many URLs at once (Free: up to 5; Pro: up to 50; Growth: up to 200; Enterprise: unlimited). The call returns immediately with a `job_id`. Poll with `get_job` or block until done with `wait_for_job`.

```python
job = wm.bulk([
    "https://example.com/article-1",
    "https://example.com/article-2",
])
job = wm.wait_for_job(job.job_id)         # blocks until status == "done"

for item in job.results:
    if item.ok:
        print(item.metadata.title)
    else:
        print(f"{item.url} failed: {item.error}")
```

`get_job` and `wait_for_job` are **polymorphic** — they work for both bulk and crawl `job_id`s. The SDK reads a `kind` discriminator from the API response and returns either a `BulkJob` or a `CrawlJob`. Use `isinstance(job, CrawlJob)` (or check `job.kind == "crawl"`) before reading crawl-specific fields like `job.truncated` or `item.depth`.

Bulk submissions carry an automatically generated `Idempotency-Key` (see [Retries & idempotency](#retries--idempotency)), so an internal retry replays the original job rather than enqueuing a second one.

## Crawl

Crawl a site BFS-style from a root URL — same-site links only, with per-plan depth and page caps (Pro: depth 5, up to 2,000 pages; Growth: depth 10, up to 10,000 pages; Enterprise: unlimited). Like `bulk`, this returns a queued job; poll with `get_job` or block until done with `wait_for_job` — the same two functions work on both kinds. Pass `render_js=True` to fetch each page through Playwright instead of httpx; a single shared browser is launched at the start of the crawl and reused across pages.

```python
job = wm.crawl("https://docs.example.com", depth=2, max_pages=500)
job = wm.wait_for_job(job.job_id)          # works for crawl AND bulk job ids

for page in job.results:
    if page.ok:
        print(f"depth={page.depth} {page.metadata.title}")
    else:
        print(f"{page.url} failed: {page.error}")

if job.truncated:
    print(f"crawl stopped early: {job.truncated_reason}")
```

`max_pages` caps how many pages this crawl may bill — it can only narrow your plan's page cap, never widen it. Each successful page consumes one request from your monthly quota — failed pages (timeouts, robots-disallowed, no-content) are not billed. If you run out of quota mid-crawl the job finishes with `truncated=True`, `truncated_reason="quota_exhausted"`.

## Retries on timeout

`extract`, `bulk`, and `crawl` take a `retry` argument — server-side re-attempts when the *target* URL times out (`target_timeout`), each on a fresh connection. Default `0` (one attempt); no upper bound, though the server stops re-attempting once a job's 6-hour lifetime is spent.

```python
wm.extract("https://slow.example.com", retry=2)     # up to 3 fetch attempts
wm.bulk(urls, retry=5)                               # async workers absorb the wait
```

On the synchronous `extract` each timed-out attempt takes 20–30s, so aggressive values belong on `bulk`/`crawl`. This is distinct from the client's own transport `max_retries` (see [Retries & idempotency](#retries--idempotency)).

## Compliance overrides

`extract`, `search`, `bulk`, and `crawl` accept three narrow-only overrides on your key's compliance policy — they can tighten it for a single request but never loosen it:

```python
wm.extract(
    "https://example.com/article",
    allow_domains=["example.com"],        # subset of what the key already allows
    deny_patterns=["*/private/*"],        # added to the key's deny list
    respect_robots="strict",              # can upgrade to strict, never down to lax
)
```

On `extract` a policy denial raises `PermissionDeniedError` (`domain_not_allowed` / `domain_denied` / `robots_disallowed`); on `bulk`/`crawl` it surfaces as a per-item `error` instead.

## Self-registration

Get a free, extract-only key from an email — no existing key needed. The zero-to-first-call path for an agent that discovered WellMarked programmatically.

```python
account = WellMarked.register("agent@example.com")
with WellMarked(api_key=account.api_key) as wm:      # Free plan, scopes=["extract"]
    print(wm.extract("https://example.com").markdown)
```

The key is deliberately weak (Free plan, extract only, no billing). To unlock bulk, crawl, search, or paid quota, a human claims the account on [wellmarked.io](https://wellmarked.io).

## Scoped keys

Mint and manage additional keys programmatically (requires a calling key with the `keys` scope). A key can only grant scopes it holds.

```python
created = wm.create_key(["extract", "bulk"], name="ci-pipeline")
print(created.api_key)                     # shown once — store it

for k in wm.list_keys():                    # metadata only, never raw values
    print(k.id, k.name, k.scopes, k.revoked_at)

wm.revoke_key(created.id)                    # stops authenticating immediately
```

Scope vocabulary: `extract`, `bulk`, `crawl`, `search`, `keys`.

## Audit log

`get_logs()` returns your own request history, newest first — which key ran each call and how its compliance policy decided.

```python
page = wm.get_logs(limit=50, offset=0)
for entry in page.logs:
    print(entry.timestamp, entry.status_code, entry.target_url, entry.policy_decision)
if page.has_more:
    ...                                      # fetch offset += limit
```

## Webhooks

Instead of polling `wait_for_job`, pass a `webhook_url` to `bulk()` or `crawl()` and we'll POST a signed notification to that URL the moment the job reaches `status="done"`.

```python
job = wm.bulk(
    ["https://example.com/article-1", "https://example.com/article-2"],
    webhook_url="https://yourapp.com/hooks/wm",
)

# First time you ever submit with a webhook_url, the response carries
# your signing secret — store it now, you only see it once.
if job.webhook_signing_secret is not None:
    save_to_env("WELLMARKED_WEBHOOK_SECRET", job.webhook_signing_secret)
```

`webhook_signing_secret` is populated **only** on the submission that first minted it (one-time visibility, Stripe-style). Subsequent submissions return `None` for that field. Lost it? Call `wm.rotate_webhook_secret()`:

```python
rotated = wm.rotate_webhook_secret()
print("New secret:", rotated.webhook_signing_secret)  # save it
```

Rotation invalidates the previous secret immediately. Deliveries already queued for retry are re-signed with the new secret on their next attempt.

### Receiving + verifying a delivery

The SDK ships a verifier — use it rather than reimplementing HMAC by hand:

```python
from fastapi import FastAPI, Request, Response
from wellmarked import verify_webhook, WebhookVerificationError

app = FastAPI()
WELLMARKED_WEBHOOK_SECRET = os.environ["WELLMARKED_WEBHOOK_SECRET"]

@app.post("/hooks/wm")
async def wellmarked_hook(request: Request):
    try:
        payload = verify_webhook(
            secret=WELLMARKED_WEBHOOK_SECRET,
            headers=request.headers,
            body=await request.body(),    # MUST be raw bytes, not request.json()
        )
    except WebhookVerificationError:
        return Response(status_code=401)

    job_id = payload["job_id"]
    # Default payload is "thin": metadata only + results_url. Fetch
    # results with the same SDK against your normal API key.
    job = wm.get_job(job_id)
    for item in job.results:
        ...
    return Response(status_code=200)
```

Pass `webhook_include_results=True` on submission to inline the full `results` array (capped at ~5 MB; over the cap the payload silently falls back to the thin shape with `results_truncated_for_size=True`).

### Delivery semantics

* Your endpoint must respond with a 2xx within **10 seconds**.
* Retries on any other outcome (timeout, 4xx, 5xx, DNS): **30s, 5m, 30m, 2h, 12h, 24h** — 7 attempts, ~38 hours, then dead-letter.
* `X-WellMarked-Delivery-Id` is stable across retries — use it as your idempotency key. `X-WellMarked-Timestamp` and `X-WellMarked-Signature` are recomputed every attempt.
* Treat delivery as **at-least-once**.

See the [Webhooks documentation](https://wellmarked.io/docs#webhooks) for the full signature scheme and header reference.

## Custom headers

Pass extra HTTP headers on every request — useful for correlation IDs, multi-tenant identifiers, or a custom user-agent suffix:

```python
with WellMarked(
    api_key="wm_...",
    headers={"X-Trace-Id": "req-abc-123", "X-Tenant": "acme"},
) as wm:
    wm.extract("https://example.com")
```

Headers can also be added or removed at runtime:

```python
wm.set_header("X-Run-Id", "run-99")
wm.extract(...)                  # carries X-Run-Id
wm.remove_header("X-Run-Id")
```

`Authorization`, `Content-Type`, and `Accept` are reserved — the SDK manages them itself, and entries passed in `headers=` for those keys are silently ignored. To rotate the bearer token, use `rotate_key()`.

## Usage & rate limits

`get_usage()` is the source of truth for your current-period quota. The quota state belongs on the account, so call `get_usage()` when you want it:

```python
usage = wm.get_usage()
print(f"{usage.used} / {usage.limit} used this period ({usage.plan}) — {usage.remaining} left")
```

`GET /usage` itself does not count toward your quota.

## Key rotation

```python
rotated = wm.rotate_key()
print("New key:", rotated.api_key)  # shown once — store it before the program exits
```

After `rotate_key()` the client automatically switches to the new key for subsequent calls; you still need to persist `rotated.api_key` somewhere durable, because the previous key stops working immediately and there is no recovery flow.

## Retries & idempotency

The client retries requests it knows are safe to replay — connection failures and 5xx, and only for GETs and POSTs carrying an `Idempotency-Key`. `bulk()` and `crawl()` send one automatically and reuse it across the SDK's internal retries, so a connection blip replays the original job rather than enqueuing a second one. `POST /extract` is never retried (the API bills it on arrival).

```python
WellMarked(api_key="wm_...", max_retries=2)   # 3 attempts total; 0 disables
```

Pass your own `idempotency_key=` to `bulk()`/`crawl()` to also survive a process crash or your own catch-and-resubmit — same key + same body replays; same key + a different body raises `idempotency_key_reuse`. Records expire after 6 hours.

## Errors

Every non-2xx response is translated into a typed exception. Catch the base class to handle anything, or the specific subclass to handle one failure mode:

```python
from wellmarked import (
    WellMarked,
    AuthenticationError,
    PermissionDeniedError,
    NotFoundError,
    UnprocessableEntityError,
    RateLimitError,
    APIConnectionError,
)

with WellMarked() as wm:
    try:
        result = wm.extract("https://example.com/paywalled")
    except RateLimitError as e:
        print(f"Quota hit. Resets in {e.retry_after}s.")
    except UnprocessableEntityError as e:
        # e.code is one of: no_content, target_timeout, target_http_error, ...
        print(f"Extraction failed ({e.code}): {e.message}")
```

| Exception                  | HTTP | Typical `code` values                                                        |
|----------------------------|------|------------------------------------------------------------------------------|
| `AuthenticationError`      | 401  | `missing_api_key`, `invalid_api_key`                                         |
| `PermissionDeniedError`    | 403  | `account_inactive`, `plan_not_supported`, `insufficient_scope`, `forbidden`, `domain_not_allowed`, `domain_denied`, `robots_disallowed` |
| `NotFoundError`            | 404  | `job_not_found`, `key_not_found`                                             |
| `UnprocessableEntityError` | 422  | `no_content`, `target_timeout`, `target_http_error`, `unsafe_url`, `bulk_cap_exceeded`, `crawl_depth_exceeded`, `idempotency_key_reuse` |
| `RateLimitError`           | 429  | `rate_limit_too_fast` *(per-second cap; `retry_after_ms` carries the sub-second back-off)* · `rate_limit_exceeded` *(monthly quota; `retry_after` in seconds)* · `register_rate_limited` |
| `InternalServerError`      | 5xx  | `service_unavailable` *(retryable)*                                          |
| `APIConnectionError`       | —    | DNS / TCP / TLS / timeout failures, raised before any HTTP round-trip        |

`APIStatusError` is the shared base of every HTTP-status error above; all inherit from `WellMarkedError`.

## Configuration

```python
WellMarked(
    api_key="wm_...",                   # or set WELLMARKED_API_KEY
    timeout=30.0,                       # seconds, per attempt
    max_retries=2,                      # transport retries for safely replayable requests
    headers={"X-Trace-Id": "..."},      # optional: extra headers on every request
)
```

The client always talks to `https://api.wellmarked.io`.

## For Agents

If you are an agent, feel free to find additional context [here](https://wellmarked.io/llms.txt)!

## License

Copyright © 2026 WellMarked. Released under the [MIT License](LICENSE).

Source: <https://github.com/WellMarkedAPI/Python-SDK>

Use of the hosted API at `api.wellmarked.io` remains subject to the
[Terms of Service](https://wellmarked.io/terms).
