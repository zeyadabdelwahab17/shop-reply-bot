"""API tests. Every model call is mocked: no real API calls, no keys needed."""

import json
import time

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import bot, main
from app.main import app
from scripts.fallback_report import load_records, summarize

client = TestClient(app)

CHAIN = [("groq", "primary"), ("gemini", "second"), ("gemini", "third")]
OK = bot.BotReply(reply="الهودي بـ 1250 جنيه", handoff=False, reason="price of P05")


def api_error(status, message):
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    cls = {429: openai.RateLimitError, 503: openai.InternalServerError, 400: openai.BadRequestError}[status]
    return cls(message, response=response, body={"error": {"message": message}})


def overloaded():
    return api_error(503, "The model is overloaded. Please try again later.")


def per_minute_limit():
    return api_error(429, "Rate limit reached on tokens per minute (TPM): Limit 8000, Used 7000")


def daily_quota():
    return api_error(429, "Rate limit reached for model on requests per day (RPD): Limit 1000, Used 1000")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Temp log file, no waits, a fixed 3-model chain, clean quota/breaker/demo-limit state."""
    monkeypatch.setattr(bot, "LOG_FILE", tmp_path / "requests.jsonl")
    monkeypatch.setattr(bot, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(bot, "CHAIN", list(CHAIN))
    bot.reset_state()
    main.reset_usage()
    yield tmp_path / "requests.jsonl"
    bot.reset_state()
    main.reset_usage()


def fake_models(monkeypatch, behaviour):
    """behaviour: {model: [exception or BotReply, ...]} consumed in order. Returns the list of models called."""
    calls = []

    def fake(provider, model, messages):
        calls.append(model)
        outcome = behaviour[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(bot, "call_model", fake)
    return calls


def last_log(path):
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


def post(message="الهودي بكام؟"):
    return client.post("/chat", json={"message": message})


# ---------- basics ----------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_serves_chat_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "ميدان ١٣" in r.text


def test_chat_returns_all_fields(monkeypatch, isolated):
    fake_models(monkeypatch, {"primary": [OK]})
    body = post().json()
    assert {k: body[k] for k in ("reply", "handoff", "reason")} == {
        "reply": "الهودي بـ 1250 جنيه", "handoff": False, "reason": "price of P05"}
    assert body["debug"]["provider"] == "groq"
    assert body["debug"]["model_used"] == "primary"
    assert body["debug"]["fallback_used"] is False
    rec = last_log(isolated)
    assert (rec["provider"], rec["model_used"], rec["reason"]) == ("groq", "primary", "price of P05")
    assert rec["attempts"] == [{"model": "groq:primary", "result": "ok", "ms": rec["attempts"][0]["ms"]}]


def test_empty_message_rejected():
    assert client.post("/chat", json={"message": "   "}).status_code == 422


# ---------- fallback chain ----------

def test_primary_fails_next_model_answers(monkeypatch, isolated):
    calls = fake_models(monkeypatch, {"primary": [api_error(400, "bad request")], "second": [OK]})
    body = post().json()
    assert body["reply"] == OK.reply
    assert calls == ["primary", "second"]  # "other" errors are not retried
    rec = last_log(isolated)
    assert rec["fallback_used"] is True
    assert (rec["provider"], rec["model_used"]) == ("gemini", "second")
    assert rec["error_type"] == "other"


def test_503_is_retried_once_then_next_model(monkeypatch, isolated):
    calls = fake_models(monkeypatch, {"primary": [overloaded(), overloaded()], "second": [OK]})
    assert post().json()["reply"] == OK.reply
    assert calls == ["primary", "primary", "second"]
    rec = last_log(isolated)
    assert rec["retries"] == 1
    assert rec["error_type"] == "503"
    assert [a["result"] for a in rec["attempts"]] == ["503", "503", "ok"]


def test_timeout_is_retried_once(monkeypatch):
    calls = fake_models(monkeypatch, {"primary": [TimeoutError("slow"), OK]})
    assert post().json()["reply"] == OK.reply
    assert calls == ["primary", "primary"]


def test_per_minute_rate_limit_goes_straight_to_next_model(monkeypatch):
    calls = fake_models(monkeypatch, {"primary": [per_minute_limit()], "second": [OK]})
    assert post().json()["reply"] == OK.reply
    assert calls == ["primary", "second"]


def test_all_models_fail_returns_handoff(monkeypatch, isolated):
    fake_models(monkeypatch, {
        "primary": [api_error(400, "bad")],
        "second": [api_error(400, "bad")],
        "third": [api_error(400, "bad")],
    })
    r = post()
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == bot.HANDOFF_MESSAGE
    assert body["handoff"] is True
    rec = last_log(isolated)
    assert rec["model_used"] is None
    assert rec["reason"] == "all models failed"
    assert body["debug"]["model_used"] is None


def test_daily_quota_no_retry_and_skipped_for_the_day(monkeypatch, isolated):
    calls = fake_models(monkeypatch, {"primary": [daily_quota()], "second": [OK, OK]})
    assert post().json()["reply"] == OK.reply
    assert calls == ["primary", "second"]  # no retry on a daily quota error
    assert "quota_skip_today:groq:primary" in last_log(isolated)["breaker_events"]

    post()  # second request: primary is not called at all
    assert calls == ["primary", "second", "second"]
    rec = last_log(isolated)
    assert rec["attempts"][0] == {"model": "groq:primary", "result": "skipped_quota", "ms": 0}
    assert rec["error_type"] is None


def test_circuit_breaker_opens_after_3_failures_in_a_row(monkeypatch, isolated):
    calls = fake_models(monkeypatch, {"primary": [overloaded(), overloaded(), overloaded()],
                                      "second": [OK, OK]})
    post()  # 2 failures (try + retry)
    post()  # 3rd failure opens the breaker -> no retry
    assert calls == ["primary", "primary", "second", "primary", "second"]
    assert "breaker_open:groq:primary" in last_log(isolated)["breaker_events"]

    fake_models(monkeypatch, {"second": [OK]})
    post()  # breaker open: primary skipped
    assert last_log(isolated)["attempts"][0]["result"] == "skipped_breaker"


def test_breaker_closes_after_cooldown(monkeypatch):
    for _ in range(3):
        bot.record_failure("groq:primary", "503")
    assert bot.skip_reason("groq:primary") == "skipped_breaker"
    bot._breaker_until["groq:primary"] = time.monotonic() - 1  # cooldown over
    assert bot.skip_reason("groq:primary") is None


def test_handoff_reply_always_contains_handoff_message(monkeypatch):
    fake_models(monkeypatch, {"primary": [bot.BotReply(reply="مش متأكد", handoff=True, reason="unsure")]})
    body = post("عايز اطبع اسمي على التيشيرت").json()
    assert body["handoff"] is True
    assert bot.HANDOFF_MESSAGE in body["reply"]


def test_slow_call_hits_hard_timeout(monkeypatch):
    monkeypatch.setattr(bot, "CALL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(bot, "complete", lambda p, m, msgs: time.sleep(1) or ('{}', {}))
    with pytest.raises(TimeoutError):
        bot.call_model("groq", "primary", [])


def test_error_types():
    assert bot.error_type(overloaded()) == "503"
    assert bot.error_type(per_minute_limit()) == "429"
    assert bot.error_type(daily_quota()) == "quota_daily"
    gemini_daily = api_error(429, "Generate content requests per day limit exceeded on Free Tier.")
    assert bot.error_type(gemini_daily) == "quota_daily"
    assert bot.error_type(TimeoutError()) == "timeout"
    assert bot.error_type(api_error(400, "bad")) == "other"
    assert bot.error_type(bot.MissingKeyError("x")) == "no_key"


def test_parse_reply_accepts_code_fence():
    r = bot.parse_reply('```json\n{"reply": "أهلا", "handoff": false, "reason": "r"}\n```')
    assert r.reply == "أهلا"


def test_parse_chain():
    assert bot.parse_chain("groq:openai/gpt-oss-120b, gemini:gemini-3.5-flash,bad:x,") == [
        ("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-3.5-flash")]


def test_reasoning_is_minimal():
    assert bot.reasoning_params("groq", "openai/gpt-oss-120b") == {"reasoning_effort": "low"}
    assert bot.reasoning_params("groq", "qwen/qwen3.8-27b") == {"reasoning_effort": "none"}
    assert bot.reasoning_params("gemini", "gemini-3.1-flash-lite") == {"reasoning_effort": "minimal"}


# ---------- demo limits ----------

def test_visitor_limit_returns_fixed_reply_without_llm(monkeypatch, isolated):
    monkeypatch.setattr(main, "DEMO_MAX_PER_VISITOR", 2)
    calls = fake_models(monkeypatch, {"primary": [OK, OK]})
    post()
    post()
    body = post().json()  # 3rd message from the same visitor
    assert body["reply"] == main.DEMO_LIMIT_REPLY
    assert body["handoff"] is False
    assert calls == ["primary", "primary"]  # no model call for the blocked message
    rec = last_log(isolated)
    assert rec["limit_hit"] == "visitor"
    assert rec["model_used"] is None


def test_global_daily_cap(monkeypatch, isolated):
    monkeypatch.setattr(main, "DEMO_DAILY_CAP", 1)
    fake_models(monkeypatch, {"primary": [OK]})
    post()
    assert main.check_demo_limit("another-visitor") == "global"
    assert post().json()["reply"] == main.DEMO_LIMIT_REPLY
    assert last_log(isolated)["limit_hit"] == "global"


# ---------- log + prompt ----------

def test_log_has_no_message_text_or_ip(monkeypatch, isolated):
    fake_models(monkeypatch, {"primary": [OK]})
    post("اسمي أحمد ورقمي 01000000000")
    text = isolated.read_text(encoding="utf-8")
    assert "أحمد" not in text and "01000000000" not in text and "testclient" not in text
    assert set(last_log(isolated)) == {
        "timestamp", "prompt_version", "model_tried_first", "provider", "model_used", "fallback_used",
        "error_type", "retries", "latency_ms", "handoff", "reason", "attempts", "breaker_events", "limit_hit",
    }


def test_bot_uses_prompt_v2_and_keeps_v1():
    assert bot.PROMPT_VERSION == "system_v2"
    assert (bot.ROOT / "app" / "prompts" / "system_v1.txt").exists()
    assert "PRODUCTS THE SHOP DOES NOT SELL" in bot.SYSTEM_PROMPT
    assert "{{SHOP_INFO}}" not in bot.SYSTEM_PROMPT


def test_prompt_shop_data_is_minified_but_keeps_facts():
    data = bot.shop_for_prompt(bot.SHOP)
    assert "_note" not in data and "payment_methods" not in data
    assert all("id" not in p and "colors" not in p for p in data["products"])
    assert all("zone" not in z and "zone_ar" in z for z in data["shipping"]["zones"])
    p = data["products"][0]
    assert {"name", "name_ar", "price_egp", "sizes", "colors_ar", "in_stock"} <= p.keys()
    assert data["handoff_message"] == bot.HANDOFF_MESSAGE
    assert "_note" in bot.SHOP and "id" in bot.SHOP["products"][0]  # the file itself is untouched
    assert bot.SYSTEM_PROMPT.startswith("You are the customer service assistant")  # static part first


def test_history_is_limited_to_last_6():
    history = [{"role": "user" if i % 2 == 0 else "bot", "text": f"msg {i}"} for i in range(10)]
    messages = bot.build_messages("new", history)
    assert len(messages) == 8  # system + 6 history + the new message
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "msg 4"
    assert messages[2]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "new"}


# ---------- fallback report ----------

def test_fallback_report_counts_fake_log(tmp_path):
    rows = [
        {"provider": "groq", "model_used": "primary", "fallback_used": False, "error_type": None, "retries": 0,
         "latency_ms": 1000, "attempts": [{"model": "groq:primary", "result": "ok", "ms": 1000}]},
        {"provider": "groq", "model_used": "primary", "fallback_used": False, "error_type": "503", "retries": 1,
         "latency_ms": 3000, "attempts": [{"model": "groq:primary", "result": "503", "ms": 500},
                                          {"model": "groq:primary", "result": "ok", "ms": 2500}]},
        {"provider": "gemini", "model_used": "second", "fallback_used": True, "error_type": "quota_daily",
         "retries": 0, "latency_ms": 5000, "breaker_events": ["quota_skip_today:groq:primary"],
         "attempts": [{"model": "groq:primary", "result": "quota_daily", "ms": 100},
                      {"model": "gemini:second", "result": "ok", "ms": 4900}]},
        {"provider": None, "model_used": None, "fallback_used": True, "error_type": "503", "retries": 1,
         "latency_ms": 9000, "breaker_events": ["breaker_open:groq:primary"],
         "attempts": [{"model": "groq:primary", "result": "skipped_quota", "ms": 0}]},
        {"model_used": None, "latency_ms": 0, "limit_hit": "visitor"},
        {"model_used": None, "latency_ms": 0, "limit_hit": "global"},
        {"model_used": "old-model", "fallback_used": False, "error_type": None, "retries": 0, "latency_ms": 2000},
    ]
    path = tmp_path / "requests.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    s = summarize(load_records(path))
    assert s["total"] == 5  # limit hits are not model requests
    assert s["main_errors"] == {"503": 2, "quota_daily": 1}
    assert s["recovered_by_retry"] == 1
    assert s["fallback_used"] == 2
    assert s["fallback_pct"] == 40.0
    assert s["failed_completely"] == 1
    assert s["answered_by"] == {"groq:primary": 2, "gemini:second": 1, "old-model": 1}
    assert s["errors_by_model"] == {"groq:primary 503": 1, "groq:primary quota_daily": 1}
    assert s["breaker_opens"] == 1
    assert s["quota_skips"] == 1
    assert s["skipped_attempts"] == {"skipped_quota": 1}
    assert s["limit_hits"] == {"visitor": 1, "global": 1}
    assert s["avg_latency_ms"] == {"groq:primary": 2000, "gemini:second": 5000, "old-model": 2000,
                                   "none (all failed)": 9000}
