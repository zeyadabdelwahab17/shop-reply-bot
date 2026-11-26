"""FastAPI app: /chat, /health, the chat page and the public /stats page."""

import hashlib
import json
import logging
import os
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from app import bot, monitoring, store

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Arabic in the Windows console
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "evals" / "results"

# Demo protection (free tiers): per-visitor and global daily caps, fixed reply without any LLM call.
DEMO_MAX_PER_VISITOR = int(os.getenv("DEMO_MAX_PER_VISITOR", "15"))
DEMO_DAILY_CAP = int(os.getenv("DEMO_DAILY_CAP", "100"))
DEMO_LIMIT_REPLY = "الديمو وصل للحد بتاعه النهارده، جرّب بكرة 🙏"

app = FastAPI(title="Midan 13 shop reply bot")


class HistoryItem(BaseModel):
    role: Literal["user", "bot"]
    text: str = Field(max_length=2000)


class ChatRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    history: list[HistoryItem] = Field(default_factory=list, max_length=50)


class DebugInfo(BaseModel):
    provider: str | None
    model_used: str | None
    latency_ms: int
    fallback_used: bool


class ChatResponse(BaseModel):
    reply: str
    handoff: bool
    reason: str
    debug: DebugInfo  # shown on the chat page only when the debug toggle is on


# ---------- demo limits ----------
# Serverless instances share no memory, so the counters live in the shared store (Upstash Redis, keys
# expire after 2 days). Visitors are stored as salted hashes of their IP, never the IP itself.
# No store configured (local dev)  -> in-memory counters with the normal caps.
# Store configured but DOWN         -> fail safe: allow the request, but count in memory with a much
#                                      lower global cap per instance (DEMO_FALLBACK_CAP).

DEMO_FALLBACK_CAP = int(os.getenv("DEMO_FALLBACK_CAP", "20"))
DAY_SECONDS = 2 * 24 * 3600

_usage_lock = threading.Lock()
_usage = {"day": None, "total": 0, "visitors": {}}


def reset_usage():
    with _usage_lock:
        _usage.update(day=None, total=0, visitors={})


def _local_limit(visitor, cap):
    with _usage_lock:
        today = date.today()
        if _usage["day"] != today:
            _usage.update(day=today, total=0, visitors={})
        if _usage["total"] >= cap:
            return "global"
        if _usage["visitors"].get(visitor, 0) >= DEMO_MAX_PER_VISITOR:
            return "visitor"
        _usage["total"] += 1
        _usage["visitors"][visitor] = _usage["visitors"].get(visitor, 0) + 1
        return None


def visitor_key(visitor):
    """Salted hash of the IP (the salt is the store's secret token, so the hash can't be looked up)."""
    salt = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    return hashlib.sha256(f"{salt}|{visitor}".encode()).hexdigest()[:24]


def _shared_limit(visitor):
    day = date.today().isoformat()
    vkey, gkey = f"srb:demo:{day}:v:{visitor_key(visitor)}", f"srb:demo:{day}:total"
    # Visitor first: someone over their own limit must not use up the global cap for everyone.
    count = int(store.run([["INCR", vkey], ["EXPIRE", vkey, DAY_SECONDS]], transaction=True)[0])
    if count > DEMO_MAX_PER_VISITOR:
        return "visitor"
    total = int(store.run([["INCR", gkey], ["EXPIRE", gkey, DAY_SECONDS]], transaction=True)[0])
    return "global" if total > DEMO_DAILY_CAP else None


def check_demo_limit(visitor):
    """Count one message. Returns None if allowed, else "visitor" or "global" (the limit that was hit)."""
    if not store.enabled():
        return _local_limit(visitor, DEMO_DAILY_CAP)
    try:
        return _shared_limit(visitor)
    except (store.StoreDown, TypeError, ValueError) as exc:
        logging.getLogger("shop_bot").warning("shared store down, conservative local demo limit: %s", exc)
        return _local_limit(visitor, min(DEMO_DAILY_CAP, DEMO_FALLBACK_CAP))


TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
ON_VERCEL = bool(os.getenv("VERCEL"))


def visitor_id(request: Request):
    """The visitor's IP, used only as a counter key (hashed before it is stored); never logged or sent.

    On Vercel: X-Real-IP. Vercel's docs: it "overwrite[s] the X-Forwarded-For header and do[es] not forward
    external IPs ... to prevent IP spoofing", and x-real-ip is "identical to the x-forwarded-for header".
    Elsewhere: proxies APPEND to X-Forwarded-For, and the left entries are whatever the client sent, so we
    count TRUSTED_PROXY_HOPS entries from the right. The global daily cap stays as the backstop.
    """
    if ON_VERCEL:
        ip = request.headers.get("x-real-ip", "").strip()
        if ip:
            return ip
    entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",") if e.strip()]
    if entries and TRUSTED_PROXY_HOPS > 0:
        return entries[-min(TRUSTED_PROXY_HOPS, len(entries))]
    return request.client.host if request.client else "unknown"


# ---------- routes ----------

def record_request(record):
    """Count it locally and send it to Langfuse BEFORE returning.

    On serverless, work after the response can be cut off (Vercel documents no after-response hook for
    Python), so the trace is sent synchronously with a short timeout (monitoring.SEND_TIMEOUT_SECONDS)
    over a kept-alive connection. Failures are logged and ignored.
    """
    monitoring.count_local(record)
    monitoring.send_trace(record)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    limit = check_demo_limit(visitor_id(request))
    if limit:
        reason = f"demo limit reached ({limit})"
        record = bot.log_request({"limit_hit": limit, "reason": reason, "handoff": False, "latency_ms": 0,
                                  "model_tried_first": None})
        record_request(record)
        return {"reply": DEMO_LIMIT_REPLY, "handoff": False, "reason": reason,
                "debug": {"provider": None, "model_used": None, "latency_ms": 0, "fallback_used": False}}

    result, meta = bot.answer(req.message, [h.model_dump() for h in req.history])
    record_request(bot.log_request(meta))
    debug = {k: meta[k] for k in ("provider", "model_used", "latency_ms", "fallback_used")}
    return {**result, "debug": debug}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- public stats ----------

def latest_evals(results_dir=RESULTS_DIR, prompt_version=None):
    """Eval numbers for /stats from the committed result files, for the live prompt version.

    main: the latest 3 runs on evals/questions.json (mean and min accuracy); holdout: the latest run.
    """
    prompt_version = prompt_version or bot.PROMPT_VERSION
    runs = {"main": [], "holdout": []}
    for path in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run = data.get("run", {})
        if run.get("prompt_version") != prompt_version:
            continue
        runs.setdefault(run.get("question_set", "main"), []).append(
            {"timestamp": run.get("timestamp"), "model": run.get("model"), **data["metrics"]})
    for v in runs.values():
        v.sort(key=lambda r: r["timestamp"] or "")
    main_runs = runs["main"][-3:]
    out = {"prompt_version": prompt_version, "main": None, "holdout": None}
    if main_runs:
        accs = [r["accuracy"] for r in main_runs]
        out["main"] = {"runs": len(main_runs), "mean": round(sum(accs) / len(accs), 1), "min": min(accs),
                       "questions": main_runs[-1]["questions"], "model": main_runs[-1]["model"],
                       "date": main_runs[-1]["timestamp"][:8]}
    if runs["holdout"]:
        h = runs["holdout"][-1]
        out["holdout"] = {"accuracy": h["accuracy"], "correct": h["correct"], "graded": h["graded"],
                          "questions": h["questions"], "model": h["model"], "date": h["timestamp"][:8]}
    return out


EVALS = latest_evals()   # files are part of the deploy, so read them once


@app.get("/stats.json")
def stats_json():
    return {"eval": EVALS, "live": monitoring.live_stats()}


@app.get("/stats")
def stats_page():
    return FileResponse(STATIC_DIR / "stats.html")
