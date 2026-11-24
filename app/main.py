"""FastAPI app: /chat, /health and the chat page."""

import logging
import sys
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from app import bot

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Arabic in the Windows console
except (AttributeError, ValueError):
    pass
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Midan 13 shop reply bot")


class HistoryItem(BaseModel):
    role: Literal["user", "bot"]
    text: str = Field(max_length=2000)


class ChatRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    history: list[HistoryItem] = Field(default_factory=list, max_length=50)


class DebugInfo(BaseModel):
    model_used: str | None
    latency_ms: int
    fallback_used: bool


class ChatResponse(BaseModel):
    reply: str
    handoff: bool
    reason: str
    debug: DebugInfo  # shown on the chat page only when the debug toggle is on


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    result, meta = bot.chat(req.message, [h.model_dump() for h in req.history])
    debug = {k: meta[k] for k in ("model_used", "latency_ms", "fallback_used")}
    return {**result, "debug": debug}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
