"""Bot logic: build the prompt, call the model chain (OpenAI-compatible APIs), log every request.

Providers (all free tiers) are called through the OpenAI chat-completions format with a custom base_url,
so Groq and Gemini use the same code. The chain is read from LLM_CHAIN in .env, e.g.
    LLM_CHAIN=groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash,gemini:gemini-3.1-flash-lite

Failure handling per model:
- daily quota used up (429 "per day")  -> no retry, skip this model for the rest of the day
- 503 / timeout                        -> retry once, then the next model
- anything else                        -> the next model
- 3 failures in a row                  -> circuit breaker: skip this model for 60 s
- all models fail                      -> the shop's handoff message (never raises)
"""

import copy
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import openai
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, PrivateAttr, ValidationError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SHOP_FILE = ROOT / "data" / "shop_info.json"
PROMPTS_DIR = ROOT / "app" / "prompts"
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "system_v2")
LOG_FILE = ROOT / "logs" / "requests.jsonl"

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY"},
}
DEFAULT_CHAIN = "gemini:gemini-3.5-flash,gemini:gemini-3.1-flash-lite"

TEMPERATURE = 0.2
MAX_OUTPUT_TOKENS = 1024
HISTORY_LIMIT = 6             # last N messages sent as conversation history
CALL_TIMEOUT_SECONDS = 12     # per model call (hard limit)
RETRY_WAIT_SECONDS = 1        # wait before the single retry on 503 / timeout
RETRYABLE_ERRORS = {"503", "timeout"}
BREAKER_THRESHOLD = 3         # failures in a row that open the breaker
BREAKER_SECONDS = 60

log = logging.getLogger("shop_bot")


def parse_chain(text):
    """"groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash" -> [("groq", "openai/gpt-oss-120b"), ...]"""
    chain = []
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        provider, _, model = item.partition(":")
        if provider in PROVIDERS and model:
            chain.append((provider, model))
        else:
            log.warning("ignoring bad LLM_CHAIN entry: %r", item)
    return chain


CHAIN = parse_chain(os.getenv("LLM_CHAIN", DEFAULT_CHAIN))

def shop_for_prompt(shop):
    """The shop data as the model sees it: without fields it never needs (saves tokens).

    Dropped: the project note, product ids, and English duplicates of colors, shipping zones and
    payment methods (the Arabic versions stay). English product names stay for Franco/English questions.
    """
    data = copy.deepcopy(shop)
    data.pop("_note", None)
    data.pop("payment_methods", None)
    for key in ("type", "physical_store", "currency"):
        data["shop"].pop(key, None)
    for product in data["products"]:
        product.pop("id", None)
        product.pop("colors", None)
    for zone in data["shipping"]["zones"]:
        zone.pop("zone", None)
    return data


# Loaded once at startup. The system prompt (rules + shop data) is the SAME string on every request and
# comes first, so providers can cache it; history and the customer message come after it.
# Compact JSON keeps it small (Groq's free tier counts tokens per minute).
SHOP = json.loads(SHOP_FILE.read_text(encoding="utf-8"))
HANDOFF_MESSAGE = SHOP["handoff_message"]


def build_system_prompt(version):
    """Rules from app/prompts/<version>.txt with the minified shop JSON inserted."""
    text = (PROMPTS_DIR / f"{version}.txt").read_text(encoding="utf-8")
    return text.replace("{{SHOP_INFO}}", json.dumps(shop_for_prompt(SHOP), ensure_ascii=False, separators=(",", ":")))


SYSTEM_PROMPT = build_system_prompt(PROMPT_VERSION)


class BotReply(BaseModel):
    reply: str
    handoff: bool
    reason: str
    _usage: dict | None = PrivateAttr(default=None)   # token counts of the call that produced it


# ---------- model calls ----------

_clients = {}


def get_client(provider):
    """One OpenAI-compatible client per provider, created on first use (tests need no keys)."""
    if provider not in _clients:
        key = os.getenv(PROVIDERS[provider]["key_env"], "").strip()
        if not key:
            raise MissingKeyError(f"{PROVIDERS[provider]['key_env']} is not set")
        _clients[provider] = OpenAI(
            api_key=key,
            base_url=PROVIDERS[provider]["base_url"],
            timeout=CALL_TIMEOUT_SECONDS,
            max_retries=0,  # our own retry logic below
        )
    return _clients[provider]


class MissingKeyError(Exception):
    pass


def reasoning_params(provider, model):
    """Lowest thinking / reasoning setting each model supports (faster, cheaper replies)."""
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}       # gpt-oss minimum
    if "qwen3" in m:
        return {"reasoning_effort": "none"}      # Qwen3 can switch thinking off
    if provider == "gemini":
        return {"reasoning_effort": "minimal"}   # Gemini 3 minimum thinking level
    return {}


def build_messages(message, history, system_prompt=None):
    """System prompt + last N history items ({"role": "user"|"bot", "text"}) + the new message."""
    messages = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    for item in (history or [])[-HISTORY_LIMIT:]:
        role = "user" if item.get("role") == "user" else "assistant"
        text = str(item.get("text", "")).strip()
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})
    return messages


def complete(provider, model, messages):
    """One raw chat-completions call. Returns (text, response headers, token usage dict or None)."""
    raw = get_client(provider).chat.completions.with_raw_response.create(
        model=model,
        messages=messages,
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
        **reasoning_params(provider, model),
    )
    response = raw.parse()
    usage = None
    if response.usage:
        usage = {"input": response.usage.prompt_tokens, "output": response.usage.completion_tokens,
                 "total": response.usage.total_tokens}
    return response.choices[0].message.content or "", raw.headers, usage


def parse_reply(text):
    """Model text -> BotReply. Accepts a ```json fence around the object."""
    text = text.strip()
    fence = re.search(r"\{.*\}", text, re.DOTALL)
    if fence:
        text = fence.group(0)
    return BotReply.model_validate_json(text)


_executor = ThreadPoolExecutor(max_workers=8)


def call_model(provider, model, messages):
    """One call with a hard time limit. Returns a BotReply or raises.

    The SDK timeout alone did not always stop slow replies, so the call runs in a worker thread
    and we stop waiting after CALL_TIMEOUT_SECONDS.
    """
    def run():
        out = complete(provider, model, messages)
        reply = parse_reply(out[0])
        reply._usage = out[2] if len(out) > 2 else None
        return reply

    future = _executor.submit(run)
    try:
        return future.result(timeout=CALL_TIMEOUT_SECONDS)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError(f"{provider}:{model} took longer than {CALL_TIMEOUT_SECONDS}s") from None


def error_type(exc):
    """Short label: "quota_daily", "429", "503", "timeout", "bad_output", "no_key" or "other"."""
    if isinstance(exc, (TimeoutError, openai.APITimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, MissingKeyError):
        return "no_key"
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return "bad_output"
    if isinstance(exc, openai.APIStatusError):
        text = str(exc).lower()
        if exc.status_code == 429:
            daily = ("per day", "perday", "(rpd)", "(tpd)", "daily")
            return "quota_daily" if any(s in text for s in daily) else "429"
        if exc.status_code == 503 or "overloaded" in text or "unavailable" in text:
            return "503"
        if exc.status_code == 504 or "deadline" in text:
            return "timeout"
    return "other"


# ---------- per-model health: daily quota + circuit breaker ----------

_state_lock = threading.Lock()
_quota_used_up = {}    # "provider:model" -> date the daily quota ran out
_failures = {}         # "provider:model" -> failures in a row
_breaker_until = {}    # "provider:model" -> time.monotonic() when the breaker closes again


def reset_state():
    """Forget quota and breaker state (used by tests)."""
    with _state_lock:
        _quota_used_up.clear()
        _failures.clear()
        _breaker_until.clear()


def skip_reason(key):
    """Why this model should be skipped right now, or None."""
    with _state_lock:
        if _quota_used_up.get(key) == date.today():
            return "skipped_quota"
        if _breaker_until.get(key, 0) > time.monotonic():
            return "skipped_breaker"
    return None


def record_success(key):
    with _state_lock:
        _failures[key] = 0
        _breaker_until.pop(key, None)


def record_failure(key, kind):
    """Count a failure. Returns breaker/quota events to log."""
    events = []
    with _state_lock:
        if kind == "quota_daily":
            _quota_used_up[key] = date.today()
            events.append(f"quota_skip_today:{key}")
        _failures[key] = _failures.get(key, 0) + 1
        if _failures[key] >= BREAKER_THRESHOLD and _breaker_until.get(key, 0) <= time.monotonic():
            _breaker_until[key] = time.monotonic() + BREAKER_SECONDS
            events.append(f"breaker_open:{key}")
    return events


# ---------- answer ----------

def answer(message, history=None):
    """Answer one customer message. Never raises.

    Returns (result, meta): result = {"reply", "handoff", "reason"}; meta = fields for the request log.
    """
    start = time.monotonic()
    messages = build_messages(message, history)
    first = f"{CHAIN[0][0]}:{CHAIN[0][1]}" if CHAIN else None
    meta = {
        "model_tried_first": first,
        "provider": None,
        "model_used": None,
        "fallback_used": False,
        "error_type": None,      # first error seen in this request
        "retries": 0,
        "attempts": [],          # [{"model": "provider:model", "result": "ok"|error|skip, "ms": int}]
        "breaker_events": [],
        "usage": None,           # token counts of the successful call
    }

    reply = None
    for provider, model in CHAIN:
        key = f"{provider}:{model}"
        skip = skip_reason(key)
        if skip:
            meta["attempts"].append({"model": key, "result": skip, "ms": 0})
            continue
        tried = 0
        while reply is None:
            t0 = time.monotonic()
            try:
                reply = call_model(provider, model, messages)
                record_success(key)
                meta["attempts"].append({"model": key, "result": "ok", "ms": _ms(t0)})
                meta.update(provider=provider, model_used=model, fallback_used=key != first,
                            usage=getattr(reply, "_usage", None))
            except Exception as exc:
                kind = error_type(exc)
                log.warning("model %s failed (%s): %s", key, kind, exc)
                meta["attempts"].append({"model": key, "result": kind, "ms": _ms(t0)})
                meta["breaker_events"] += record_failure(key, kind)
                meta["error_type"] = meta["error_type"] or kind
                if kind in RETRYABLE_ERRORS and tried == 0 and not skip_reason(key):
                    tried = 1
                    meta["retries"] += 1
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                break
        if reply is not None:
            break

    if reply is None:
        result = {"reply": HANDOFF_MESSAGE, "handoff": True, "reason": "all models failed"}
    else:
        result = finalize(reply)

    meta["latency_ms"] = _ms(start)
    meta["handoff"] = result["handoff"]
    meta["reason"] = result["reason"]
    log.info("answered by %s:%s (fallback=%s, latency=%sms)",
             meta["provider"], meta["model_used"], meta["fallback_used"], meta["latency_ms"])
    return result, meta


def finalize(reply):
    """BotReply -> result dict. A handoff must always show the shop's exact handoff message."""
    result = reply.model_dump()
    if result["handoff"] and HANDOFF_MESSAGE not in result["reply"]:
        result["reply"] = (result["reply"].strip() + "\n" + HANDOFF_MESSAGE).strip()
    return result


def _ms(t0):
    return round((time.monotonic() - t0) * 1000)


# ---------- request log ----------

_log_lock = threading.Lock()


def log_request(meta):
    """Append one line to logs/requests.jsonl. No key, no message text, no IP; "reason" is the model's
    short English note (the prompt forbids personal details in it).

    Kept in one place so Day 4 can send the same record to Langfuse.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "model_tried_first": meta.get("model_tried_first"),
        "provider": meta.get("provider"),
        "model_used": meta.get("model_used"),
        "fallback_used": meta.get("fallback_used", False),
        "error_type": meta.get("error_type"),
        "retries": meta.get("retries", 0),
        "latency_ms": meta.get("latency_ms", 0),
        "handoff": meta.get("handoff", False),
        "reason": meta.get("reason"),
        "attempts": meta.get("attempts", []),
        "breaker_events": meta.get("breaker_events", []),
        "limit_hit": meta.get("limit_hit"),   # "visitor" | "global" | None
        "usage": meta.get("usage"),           # {"input", "output", "total"} tokens
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
