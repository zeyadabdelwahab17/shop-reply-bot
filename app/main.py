"""FastAPI app: /chat, /health, the chat page and the public /stats page."""

import json
import logging
import os
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from app import bot, monitoring

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


# ---------- demo limits (in memory; reset at midnight and on restart) ----------

_usage_lock = threading.Lock()
_usage = {"day": None, "total": 0, "visitors": {}}


def reset_usage():
    with _usage_lock:
        _usage.update(day=None, total=0, visitors={})


def check_demo_limit(visitor):
    """Count one message. Returns None if allowed, else "visitor" or "global" (the limit that was hit)."""
    with _usage_lock:
        today = date.today()
        if _usage["day"] != today:
            _usage.update(day=today, total=0, visitors={})
        if _usage["total"] >= DEMO_DAILY_CAP:
            return "global"
        if _usage["visitors"].get(visitor, 0) >= DEMO_MAX_PER_VISITOR:
            return "visitor"
        _usage["total"] += 1
        _usage["visitors"][visitor] = _usage["visitors"].get(visitor, 0) + 1
        return None


TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
IP_DEBUG = os.getenv("IP_DEBUG", "") == "1"


def visitor_id(request: Request):
    """The visitor's IP, used only as an in-memory counter key; it is never logged or sent anywhere.

    Behind a proxy, request.client is the proxy. The left entries of X-Forwarded-For are whatever the
    client sent (easy to fake); each proxy APPENDS the address it saw. So we count TRUSTED_PROXY_HOPS
    entries from the right: with 1 trusted proxy, the last entry is the one our proxy added.
    Render's docs don't document its header behaviour: check it once with IP_DEBUG=1 (see /debug/ip).
    The global daily cap (DEMO_DAILY_CAP) stays as the backstop.
    """
    entries = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",") if e.strip()]
    if entries and TRUSTED_PROXY_HOPS > 0:
        return entries[-min(TRUSTED_PROXY_HOPS, len(entries))]
    return request.client.host if request.client else "unknown"


# ---------- routes ----------

def record_request(record, background: BackgroundTasks):
    """Count it locally and send it to Langfuse after the response (never slows the reply)."""
    monitoring.count_local(record)
    background.add_task(monitoring.send_trace, record)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, background: BackgroundTasks):
    limit = check_demo_limit(visitor_id(request))
    if limit:
        reason = f"demo limit reached ({limit})"
        record = bot.log_request({"limit_hit": limit, "reason": reason, "handoff": False, "latency_ms": 0,
                                  "model_tried_first": None})
        record_request(record, background)
        return {"reply": DEMO_LIMIT_REPLY, "handoff": False, "reason": reason,
                "debug": {"provider": None, "model_used": None, "latency_ms": 0, "fallback_used": False}}

    result, meta = bot.answer(req.message, [h.model_dump() for h in req.history])
    record_request(bot.log_request(meta), background)
    debug = {k: meta[k] for k in ("provider", "model_used", "latency_ms", "fallback_used")}
    return {**result, "debug": debug}


@app.get("/debug/ip")
def debug_ip(request: Request):
    """Off unless IP_DEBUG=1. Echoes only the CALLER's own proxy headers back to the caller (nothing is
    logged), to check once which X-Forwarded-For entry the hosting proxy adds."""
    if not IP_DEBUG:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    names = ("x-forwarded-for", "true-client-ip", "cf-connecting-ip", "x-real-ip", "x-forwarded-proto")
    return {"headers": {n: request.headers.get(n) for n in names},
            "client_host": request.client.host if request.client else None,
            "visitor_id_used": visitor_id(request), "trusted_proxy_hops": TRUSTED_PROXY_HOPS}


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
