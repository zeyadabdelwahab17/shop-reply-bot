"""API tests. The Gemini call is mocked: no real API calls, no key needed."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from app import bot
from app.main import app
from scripts.fallback_report import load_records, summarize

client = TestClient(app)


def overloaded():
    return errors.ServerError(503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}})


def rate_limited():
    return errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded.", "status": "RESOURCE_EXHAUSTED"}})


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Write logs to a temp file, skip the retry wait, use fixed model names."""
    monkeypatch.setattr(bot, "LOG_FILE", tmp_path / "requests.jsonl")
    monkeypatch.setattr(bot, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(bot, "MAIN_MODEL", "main-model")
    monkeypatch.setattr(bot, "FALLBACK_MODEL", "fallback-model")
    return tmp_path / "requests.jsonl"


def fake_gemini(monkeypatch, behaviour):
    """behaviour: {model: [exception or BotReply, ...]} consumed in order. Returns the list of models called."""
    calls = []

    def fake(model, contents):
        calls.append(model)
        outcome = behaviour[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(bot, "call_gemini", fake)
    return calls


def last_log(path):
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


OK = bot.BotReply(reply="الهودي بـ 1250 جنيه", handoff=False, reason="price of P05")


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_serves_chat_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "ميدان ١٣" in r.text


def test_chat_returns_all_fields(monkeypatch, isolated):
    fake_gemini(monkeypatch, {"main-model": [OK]})
    r = client.post("/chat", json={"message": "الهودي بكام؟", "history": []})
    assert r.status_code == 200
    assert r.json() == {"reply": "الهودي بـ 1250 جنيه", "handoff": False, "reason": "price of P05"}
    rec = last_log(isolated)
    assert rec["model_used"] == "main-model"
    assert rec["fallback_used"] is False
    assert rec["error_type"] is None
    assert rec["prompt_version"] == bot.PROMPT_VERSION


def test_503_on_main_model_triggers_fallback(monkeypatch, isolated):
    calls = fake_gemini(monkeypatch, {"main-model": [overloaded(), overloaded()], "fallback-model": [OK]})
    r = client.post("/chat", json={"message": "الهودي بكام؟"})
    assert r.status_code == 200
    assert r.json()["reply"] == OK.reply
    assert calls == ["main-model", "main-model", "fallback-model"]  # one retry, then fallback
    rec = last_log(isolated)
    assert rec["fallback_used"] is True
    assert rec["model_used"] == "fallback-model"
    assert rec["error_type"] == "503"
    assert rec["retries"] == 1


def test_429_recovers_on_retry_without_fallback(monkeypatch, isolated):
    calls = fake_gemini(monkeypatch, {"main-model": [rate_limited(), OK]})
    r = client.post("/chat", json={"message": "الهودي بكام؟"})
    assert r.json()["reply"] == OK.reply
    assert calls == ["main-model", "main-model"]
    rec = last_log(isolated)
    assert rec["fallback_used"] is False
    assert rec["error_type"] == "429"
    assert rec["retries"] == 1


def test_both_models_failing_returns_handoff(monkeypatch, isolated):
    fake_gemini(monkeypatch, {
        "main-model": [overloaded(), overloaded()],
        "fallback-model": [TimeoutError("read timed out")],
    })
    r = client.post("/chat", json={"message": "الهودي بكام؟"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == bot.HANDOFF_MESSAGE
    assert body["handoff"] is True
    rec = last_log(isolated)
    assert rec["model_used"] is None
    assert rec["fallback_used"] is True
    assert rec["handoff"] is True


def test_handoff_reply_always_contains_handoff_message(monkeypatch):
    fake_gemini(monkeypatch, {"main-model": [bot.BotReply(reply="مش متأكد", handoff=True, reason="unsure")]})
    body = client.post("/chat", json={"message": "عندكم كوتشيات؟"}).json()
    assert body["handoff"] is True
    assert bot.HANDOFF_MESSAGE in body["reply"]


def test_log_has_no_message_text(monkeypatch, isolated):
    fake_gemini(monkeypatch, {"main-model": [OK]})
    client.post("/chat", json={"message": "اسمي أحمد ورقمي 01000000000"})
    text = isolated.read_text(encoding="utf-8")
    assert "أحمد" not in text and "01000000000" not in text
    assert set(last_log(isolated)) == {
        "timestamp", "prompt_version", "model_tried_first", "model_used", "fallback_used",
        "error_type", "retries", "latency_ms", "handoff",
    }


def test_slow_call_hits_hard_timeout(monkeypatch):
    monkeypatch.setattr(bot, "CALL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(bot, "_generate", lambda model, contents: time.sleep(1) or OK)
    with pytest.raises(TimeoutError):
        bot.call_gemini("main-model", [])


def test_error_types():
    assert bot.error_type(overloaded()) == "503"
    assert bot.error_type(rate_limited()) == "429"
    assert bot.error_type(TimeoutError()) == "timeout"
    deadline = errors.ServerError(504, {"error": {"code": 504, "message": "Deadline expired", "status": "DEADLINE_EXCEEDED"}})
    assert bot.error_type(deadline) == "timeout"
    gone = errors.ClientError(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
    assert bot.error_type(gone) == "other"


def test_empty_message_rejected():
    assert client.post("/chat", json={"message": "   "}).status_code == 422


def test_history_is_limited_to_last_6():
    history = [{"role": "user" if i % 2 == 0 else "bot", "text": f"msg {i}"} for i in range(10)]
    contents = bot.build_contents("new", history)
    assert len(contents) == 7  # 6 history + the new message
    assert contents[0].parts[0].text == "msg 4"
    assert contents[1].role == "model"
    assert contents[-1].parts[0].text == "new"


def test_fallback_report_counts_fake_log(tmp_path):
    rows = [
        {"model_used": "main-model", "fallback_used": False, "error_type": None, "retries": 0, "latency_ms": 1000, "handoff": False},
        {"model_used": "main-model", "fallback_used": False, "error_type": "429", "retries": 1, "latency_ms": 3000, "handoff": False},
        {"model_used": "fallback-model", "fallback_used": True, "error_type": "503", "retries": 1, "latency_ms": 4000, "handoff": False},
        {"model_used": "fallback-model", "fallback_used": True, "error_type": "503", "retries": 1, "latency_ms": 6000, "handoff": True},
        {"model_used": None, "fallback_used": True, "error_type": "timeout", "retries": 0, "latency_ms": 40000, "handoff": True},
    ]
    path = tmp_path / "requests.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    s = summarize(load_records(path))
    assert s["total"] == 5
    assert s["main_errors"] == {"429": 1, "503": 2, "timeout": 1}
    assert s["main_errors_total"] == 4
    assert s["recovered_by_retry"] == 1
    assert s["fallback_used"] == 3
    assert s["fallback_pct"] == 60.0
    assert s["failed_completely"] == 1
    assert s["avg_latency_ms"] == {"main-model": 2000, "fallback-model": 5000, "none (all failed)": 40000}
