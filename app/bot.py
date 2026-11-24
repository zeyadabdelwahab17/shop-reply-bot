"""Bot logic: build the prompt, call Gemini (with retry + fallback), log every request."""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SHOP_FILE = ROOT / "data" / "shop_info.json"
PROMPT_VERSION = "system_v2"
PROMPT_FILE = ROOT / "app" / "prompts" / f"{PROMPT_VERSION}.txt"
LOG_FILE = ROOT / "logs" / "requests.jsonl"

MAIN_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
TEMPERATURE = 0.2
HISTORY_LIMIT = 6           # last N messages sent as conversation history
CALL_TIMEOUT_SECONDS = 20   # per Gemini call
RETRY_WAIT_SECONDS = 2      # wait before retrying the main model once
RETRYABLE_ERRORS = {"503", "429"}

log = logging.getLogger("shop_bot")

# Loaded once at startup.
SHOP = json.loads(SHOP_FILE.read_text(encoding="utf-8"))
HANDOFF_MESSAGE = SHOP["handoff_message"]
SYSTEM_PROMPT = PROMPT_FILE.read_text(encoding="utf-8").replace(
    "{{SHOP_INFO}}", json.dumps(SHOP, ensure_ascii=False, indent=1)
)


class BotReply(BaseModel):
    reply: str
    handoff: bool
    reason: str


_client = None


def get_client():
    """Create the Gemini client on first use (so tests and imports need no key)."""
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=os.getenv("GEMINI_API_KEY"),
            http_options=types.HttpOptions(
                timeout=CALL_TIMEOUT_SECONDS * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),  # our own retry logic below
            ),
        )
    return _client


def build_contents(message, history):
    """Turn [{"role": "user"|"bot", "text": ...}] + the new message into Gemini contents."""
    contents = []
    for item in (history or [])[-HISTORY_LIMIT:]:
        role = "user" if item.get("role") == "user" else "model"
        text = str(item.get("text", "")).strip()
        if text:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
    return contents


_executor = ThreadPoolExecutor(max_workers=8)


def call_gemini(model, contents):
    """One call to Gemini with a hard time limit. Returns a BotReply or raises.

    The SDK timeout alone did not stop slow replies (calls of 120s+ were seen), so the call
    runs in a worker thread and we stop waiting after CALL_TIMEOUT_SECONDS.
    """
    future = _executor.submit(_generate, model, contents)
    try:
        return future.result(timeout=CALL_TIMEOUT_SECONDS)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError(f"{model} took longer than {CALL_TIMEOUT_SECONDS}s") from None


def _generate(model, contents):
    response = get_client().models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=TEMPERATURE,
            response_mime_type="application/json",
            response_schema=BotReply,
        ),
    )
    if isinstance(response.parsed, BotReply):
        return response.parsed
    return BotReply.model_validate_json(response.text)


def error_type(exc):
    """Short label for an error: "503", "429", "timeout" or "other"."""
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, errors.APIError):
        status = str(getattr(exc, "status", "") or "").upper()
        if exc.code == 504 or status == "DEADLINE_EXCEEDED":
            return "timeout"
        if exc.code == 503 or status == "UNAVAILABLE" or "overloaded" in str(exc).lower():
            return "503"
        if exc.code == 429 or status == "RESOURCE_EXHAUSTED":
            return "429"
    return "other"


def answer(message, history=None):
    """Answer one customer message. Never raises.

    Returns (result, meta): result = {"reply", "handoff", "reason"}; meta = fields for the request log.
    """
    start = time.monotonic()
    contents = build_contents(message, history)
    meta = {
        "model_tried_first": MAIN_MODEL,
        "model_used": None,
        "fallback_used": False,
        "error_type": None,  # first error seen on the main model
        "retries": 0,
    }

    # Main model, with one retry on 503 / 429; then the fallback model once.
    attempts = [MAIN_MODEL]
    reply = None
    while attempts:
        model = attempts.pop(0)
        try:
            reply = call_gemini(model, contents)
            meta["model_used"] = model
            break
        except Exception as exc:
            kind = error_type(exc)
            log.warning("model %s failed (%s): %s", model, kind, exc)
            if model == MAIN_MODEL and meta["error_type"] is None:
                meta["error_type"] = kind
            if model == MAIN_MODEL and meta["retries"] == 0 and kind in RETRYABLE_ERRORS:
                meta["retries"] = 1
                time.sleep(RETRY_WAIT_SECONDS)
                attempts.append(MAIN_MODEL)
            elif model == MAIN_MODEL and FALLBACK_MODEL and FALLBACK_MODEL != MAIN_MODEL:
                meta["fallback_used"] = True
                attempts.append(FALLBACK_MODEL)

    if reply is None:
        result = {"reply": HANDOFF_MESSAGE, "handoff": True, "reason": "all models failed"}
    else:
        result = reply.model_dump()
        # A handoff must always show the shop's exact handoff message.
        if result["handoff"] and HANDOFF_MESSAGE not in result["reply"]:
            result["reply"] = (result["reply"].strip() + "\n" + HANDOFF_MESSAGE).strip()

    meta["latency_ms"] = round((time.monotonic() - start) * 1000)
    meta["handoff"] = result["handoff"]
    meta["reason"] = result["reason"]
    log.info("answered by %s (fallback=%s, latency=%sms)", meta["model_used"], meta["fallback_used"], meta["latency_ms"])
    return result, meta


_log_lock = threading.Lock()


def log_request(meta):
    """Append one line to logs/requests.jsonl. No key and no message text; "reason" is the model's short
    English note (the prompt forbids personal details in it).

    Kept in one place so Day 4 can send the same record to Langfuse.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "model_tried_first": meta["model_tried_first"],
        "model_used": meta["model_used"],
        "fallback_used": meta["fallback_used"],
        "error_type": meta["error_type"],
        "retries": meta["retries"],
        "latency_ms": meta["latency_ms"],
        "handoff": meta["handoff"],
        "reason": meta["reason"],
    }
    with _log_lock:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def chat(message, history=None):
    """Answer and log. Used by the /chat endpoint and by scripts/try_bot.py."""
    result, meta = answer(message, history)
    log_request(meta)
    return result, meta
