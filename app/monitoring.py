"""Langfuse monitoring (free Hobby plan) over plain HTTP, plus in-memory fallback counters.

Langfuse Cloud removed its v3 REST APIs on 16 Nov 2026, so this uses the v4 path:
- send_trace(record): one trace per /chat request, sent as OpenTelemetry (OTLP/HTTP JSON) to
  /api/public/otel/v1/traces with "x-langfuse-ingestion-version: 4" (real-time on the v2 read APIs).
  A root span "chat" + a child "llm" generation (model, token counts) when a model answered.
  Only metadata from bot.log_request(): prompt version, model, provider, fallback, breaker events, latency,
  handoff, reason, demo-limit hits, tokens. NO message text, so no phone numbers or names can reach Langfuse.
- live_stats(): counters for /stats, read from GET /api/public/v2/observations (root observations of this
  environment). Without keys, or if Langfuse is unreachable: memory counters since the last restart.

Keys come from the environment only: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST.
Empty keys = monitoring off, quietly. Nothing here ever raises into the request.
"""

import json
import logging
import os
import secrets
import ssl
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("shop_bot.monitoring")

TRACE_NAME = "chat"
SERVICE = "shop-reply-bot"
APP_ENV = os.getenv("APP_ENV", "local")        # "production" on Render; the stats page counts only these
STATS_CACHE_SECONDS = 60
PAGE_LIMIT = 1000                              # Langfuse max per page
MAX_PAGES = 5                                  # 5000 requests; the demo cap is 150 requests/day
STARTED_AT = datetime.now(timezone.utc)
INGESTION_HEADERS = {"x-langfuse-ingestion-version": "4"}
# Sent before the reply returns (serverless can cut off after-response work), so keep it short.
SEND_TIMEOUT_SECONDS = 2.0
# Verify TLS against the operating system's trust store (Windows store locally, system CAs on Render)
# instead of httpx's bundled list, so it also works behind antivirus HTTPS scanning. Verification stays on.
TLS = ssl.create_default_context()

METADATA_KEYS = ("prompt_version", "provider", "model_used", "model_tried_first", "fallback_used", "error_type",
                 "retries", "latency_ms", "handoff", "reason", "breaker_events", "limit_hit", "attempts")


def _config():
    pk, sk = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip(), os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "").strip().rstrip("/") or "https://cloud.langfuse.com"
    return (pk, sk, host) if pk and sk else None


def enabled():
    return _config() is not None


# ---------- sending (OTLP/HTTP JSON) ----------

def _text(value):
    """Langfuse metadata attributes are strings: lists/dicts as JSON, booleans as "true"/"false"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _attr(key, value):
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return {"key": key, "value": {"arrayValue": {"values": [{"stringValue": v} for v in value]}}}
    return {"key": key, "value": {"stringValue": _text(value)}}


def build_otlp(record, now=None):
    """OTLP/JSON body for one request record (see bot.log_request). Pure, for tests."""
    now = now or datetime.now(timezone.utc)
    end_ns = int(now.timestamp() * 1e9)
    start_ns = end_ns - int((record.get("latency_ms") or 0) * 1e6)
    trace_id, root_id = secrets.token_hex(16), secrets.token_hex(8)

    tags = [APP_ENV, record.get("prompt_version") or "no-prompt"]
    if record.get("model_used"):
        tags.append(record["model_used"])
    tags += [t for t, on in (("handoff", record.get("handoff")), ("fallback", record.get("fallback_used")),
                             ("limit_hit", record.get("limit_hit"))) if on]
    meta = {k: record.get(k) for k in METADATA_KEYS if record.get(k) is not None}

    # Trace-level attributes go on EVERY span (Langfuse needs that to filter/aggregate on them).
    shared = [_attr("langfuse.trace.name", TRACE_NAME), _attr("langfuse.trace.tags", tags),
              _attr("langfuse.environment", APP_ENV)]
    shared += [_attr(f"langfuse.trace.metadata.{k}", v) for k, v in meta.items()]

    root = {"traceId": trace_id, "spanId": root_id, "name": TRACE_NAME, "kind": 2,
            "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(end_ns),
            "attributes": shared + [_attr("langfuse.observation.type", "span")]
                          + [_attr(f"langfuse.observation.metadata.{k}", v) for k, v in meta.items()]}
    spans = [root]
    if record.get("model_used"):
        usage = {k: v for k, v in (record.get("usage") or {}).items() if v is not None}
        gen = [_attr("langfuse.observation.type", "generation"),
               _attr("langfuse.observation.model.name", record["model_used"]),
               _attr("langfuse.observation.metadata.provider", record.get("provider") or "")]
        if usage:
            gen.append(_attr("langfuse.observation.usage_details", usage))
        spans.append({"traceId": trace_id, "spanId": secrets.token_hex(8), "parentSpanId": root_id,
                      "name": "llm", "kind": 3, "startTimeUnixNano": str(start_ns),
                      "endTimeUnixNano": str(end_ns), "attributes": shared + gen})
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", SERVICE)]},
        "scopeSpans": [{"scope": {"name": SERVICE}, "spans": spans}]}]}


_send_client = None


def _sender():
    """One client per instance, so warm instances reuse the TLS connection to Langfuse."""
    global _send_client
    if _send_client is None:
        _send_client = httpx.Client(timeout=SEND_TIMEOUT_SECONDS, verify=TLS)
    return _send_client


def send_trace(record):
    """POST the trace to Langfuse (synchronously, short timeout). Never raises."""
    cfg = _config()
    if not cfg:
        return
    pk, sk, host = cfg
    try:
        r = _sender().post(f"{host}/api/public/otel/v1/traces", auth=(pk, sk),
                           headers=INGESTION_HEADERS, json=build_otlp(record))
        if r.status_code >= 300:
            log.warning("langfuse ingestion: HTTP %s %s", r.status_code, r.text[:300])
    except Exception as exc:  # never break the chat because of monitoring
        log.warning("langfuse ingestion failed: %s", exc)


# ---------- counters for /stats ----------

_lock = threading.Lock()
_local = {"requests": 0, "fallback": 0, "handoff": 0, "limit_hits": 0, "latency_sum": 0, "answered": 0}
_cache = {"at": 0.0, "value": None}


def count_local(record):
    """In-memory counters since the last restart (used when Langfuse is not configured or not reachable)."""
    with _lock:
        _local["requests"] += 1
        if record.get("limit_hit"):
            _local["limit_hits"] += 1
            return
        _local["answered"] += 1
        _local["fallback"] += bool(record.get("fallback_used"))
        _local["handoff"] += bool(record.get("handoff"))
        _local["latency_sum"] += record.get("latency_ms") or 0


def reset_local():
    with _lock:
        for k in _local:
            _local[k] = 0
        _cache.update(at=0.0, value=None)


def _yes(value):
    """Metadata comes back from Langfuse as strings: "true" / "false"."""
    return value is True or str(value).strip().lower() == "true"


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize(metas):
    """Counter dict from a list of metadata dicts. Demo-limit hits are not model answers."""
    answered = [m for m in metas if not m.get("limit_hit")]
    n = len(answered)
    return {
        "requests": len(metas),
        "fallback_pct": round(100 * sum(_yes(m.get("fallback_used")) for m in answered) / n, 1) if n else None,
        "handoff_pct": round(100 * sum(_yes(m.get("handoff")) for m in answered) / n, 1) if n else None,
        "avg_latency_ms": round(sum(_number(m.get("latency_ms")) for m in answered) / n) if n else None,
        "limit_hits": len(metas) - n,
    }


def _local_stats():
    with _lock:
        c = dict(_local)
    n = c["answered"]
    return {
        "requests": c["requests"],
        "fallback_pct": round(100 * c["fallback"] / n, 1) if n else None,
        "handoff_pct": round(100 * c["handoff"] / n, 1) if n else None,
        "avg_latency_ms": round(c["latency_sum"] / n) if n else None,
        "limit_hits": c["limit_hits"],
        "source": "memory",
        "since": STARTED_AT.isoformat(timespec="minutes"),
        "note": "Counted in memory by this server instance only (serverless: not shared, resets often).",
    }


def _langfuse_stats(cfg):
    """One metadata dict per request: the root "chat" observations of this environment (v2 API, cursor pages)."""
    pk, sk, host = cfg
    params = {"name": TRACE_NAME, "environment": APP_ENV, "isRootObservation": "true",
              "fields": "core,basic,metadata", "limit": PAGE_LIMIT}
    metas, first, truncated = [], None, False
    with httpx.Client(auth=(pk, sk), timeout=8, verify=TLS) as client:
        for page in range(MAX_PAGES):
            r = client.get(f"{host}/api/public/v2/observations", params=params)
            r.raise_for_status()
            body = r.json()
            for row in body.get("data", []):
                metas.append(row.get("metadata") or {})
                start = row.get("startTime")
                if start and (first is None or start < first):
                    first = start
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                break
            params["cursor"] = cursor
        else:
            truncated = True
    stats = summarize(metas)
    stats.update(source="langfuse", since=first,
                 note="From Langfuse: all production requests since launch (survives restarts).")
    if truncated:
        stats["note"] += f" Only the latest {MAX_PAGES * PAGE_LIMIT} requests are counted."
    return stats


def live_stats():
    """Counters for /stats, cached for a minute so the page doesn't hit Langfuse's API limits."""
    now = time.monotonic()
    with _lock:
        if _cache["value"] and now - _cache["at"] < STATS_CACHE_SECONDS:
            return _cache["value"]
    cfg = _config()
    value = None
    if cfg:
        try:
            value = _langfuse_stats(cfg)
        except Exception as exc:
            log.warning("langfuse stats failed, using memory counters: %s", exc)
    value = value or _local_stats()
    with _lock:
        _cache.update(at=now, value=value)
    return value
