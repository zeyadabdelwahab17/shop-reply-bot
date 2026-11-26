"""Shared state for serverless: Upstash Redis (free plan) over its REST API, plain httpx.

Serverless instances share no memory, so the demo counters and the model health flags (circuit breaker,
"daily quota used up") live here. Keys expire on their own; no cleanup job is needed.

Env: UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN. Not set → enabled() is False and the callers use
their in-memory fallback. Every call can raise StoreDown; callers must fail safe.
"""

import logging
import os
import ssl

import httpx

log = logging.getLogger("shop_bot.store")

TIMEOUT_SECONDS = 1.5          # the store must never noticeably slow a reply
TLS = ssl.create_default_context()   # OS trust store (works behind antivirus HTTPS scanning too)

_client = None


class StoreDown(Exception):
    pass


def _config():
    url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    return (url, token) if url and token else None


def enabled():
    return _config() is not None


def _http():
    """One client per instance: warm instances reuse the TLS connection (~1 round trip per call)."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=TIMEOUT_SECONDS, verify=TLS)
    return _client


def run(commands, transaction=False):
    """Run several Redis commands in one HTTP request. Returns the list of results.

    commands: [["INCR", "k"], ["EXPIRE", "k", 3600]]. transaction=True uses /multi-exec (atomic).
    """
    cfg = _config()
    if not cfg:
        raise StoreDown("store not configured")
    url, token = cfg
    try:
        r = _http().post(f"{url}/{'multi-exec' if transaction else 'pipeline'}",
                         headers={"Authorization": f"Bearer {token}"}, json=commands)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        raise StoreDown(str(exc)[:200]) from None
    if isinstance(body, dict) and "error" in body:   # whole request rejected
        raise StoreDown(body["error"])
    results = []
    for item in body:
        if "error" in item:
            raise StoreDown(item["error"])
        results.append(item.get("result"))
    return results
