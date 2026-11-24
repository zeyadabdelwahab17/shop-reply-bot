"""FastAPI app: /chat, /health and the chat page."""

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

from app import bot

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Arabic in the Windows console
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Demo protection (free tiers): per-visitor and global daily caps, fixed reply without any LLM call.
DEMO_MAX_PER_VISITOR = int(os.getenv("DEMO_MAX_PER_VISITOR", "15"))
DEMO_DAILY_CAP = int(os.getenv("DEMO_DAILY_CAP", "150"))
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


def visitor_id(request: Request):
    # The visitor's IP is used only as an in-memory counter key; it is never logged.
    # TODO Day 4: behind Cloud Run's proxy, check that this is the real client IP.
    return request.client.host if request.client else "unknown"


# ---------- routes ----------

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    limit = check_demo_limit(visitor_id(request))
    if limit:
        reason = f"demo limit reached ({limit})"
        bot.log_request({"limit_hit": limit, "reason": reason, "handoff": False, "latency_ms": 0,
                         "model_tried_first": None})
        return {"reply": DEMO_LIMIT_REPLY, "handoff": False, "reason": reason,
                "debug": {"provider": None, "model_used": None, "latency_ms": 0, "fallback_used": False}}

    result, meta = bot.chat(req.message, [h.model_dump() for h in req.history])
    debug = {k: meta[k] for k in ("provider", "model_used", "latency_ms", "fallback_used")}
    return {**result, "debug": debug}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
