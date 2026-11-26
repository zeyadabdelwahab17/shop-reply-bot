"""Serverless behaviour: shared store (fake Redis), fail-safe demo limits, request time budget, read-only disk."""

import time

import pytest
from fastapi.testclient import TestClient

from app import bot, main, monitoring, store
from app.main import app

client = TestClient(app)
OK = bot.BotReply(reply="الهودي بـ 1250 جنيه", handoff=False, reason="price")


class FakeRedis:
    """Just the commands the app uses, with the same results Upstash returns."""

    def __init__(self):
        self.data, self.ttl, self.calls, self.down = {}, {}, [], False

    def run(self, commands, transaction=False):
        self.calls.append(commands)
        if self.down:
            raise store.StoreDown("connection refused")
        out = []
        for cmd, *args in commands:
            key = args[0]
            if cmd == "INCR":
                self.data[key] = int(self.data.get(key, 0)) + 1
                out.append(self.data[key])
            elif cmd == "EXPIRE":
                self.ttl[key] = args[1]
                out.append(1)
            elif cmd == "SET":
                self.data[key] = args[1]
                if "EX" in args:
                    self.ttl[key] = args[args.index("EX") + 1]
                out.append("OK")
            elif cmd == "GET":
                out.append(None if key not in self.data else str(self.data[key]))
            elif cmd == "EXISTS":
                out.append(1 if key in self.data else 0)
            elif cmd == "PTTL":
                out.append(self.ttl[key] * 1000 if key in self.data and key in self.ttl else -2)
            elif cmd == "DEL":
                out.append(1 if self.data.pop(key, None) is not None else 0)
            else:
                raise AssertionError(f"unexpected command {cmd}")
        return out


@pytest.fixture
def redis(monkeypatch, tmp_path):
    fake = FakeRedis()
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.test")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "secret-token")
    monkeypatch.setattr(store, "run", fake.run)
    monkeypatch.setattr(bot, "LOG_FILE", tmp_path / "requests.jsonl")
    monkeypatch.setattr(bot, "CHAIN", [("groq", "primary"), ("gemini", "second")])
    monkeypatch.setattr(bot, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(monitoring, "send_trace", lambda record: None)
    main.reset_usage()
    yield fake
    main.reset_usage()


def chat(ip="203.0.113.7"):
    return client.post("/chat", json={"message": "بكام؟"}, headers={"X-Forwarded-For": ip}).json()


# ---------- demo limits in the shared store ----------

def test_limits_are_counted_in_the_store_with_hashed_ips(monkeypatch, redis):
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    assert chat()["reply"] == OK.reply
    keys = list(redis.data)
    assert any(":total" in k for k in keys) and any(":v:" in k for k in keys)
    assert not any("203.0.113.7" in k for k in keys)        # the IP itself is never stored
    assert all(redis.ttl[k] == main.DAY_SECONDS for k in keys if k.startswith("srb:demo:"))


def test_visitor_limit_is_shared_across_instances(monkeypatch, redis):
    monkeypatch.setattr(main, "DEMO_MAX_PER_VISITOR", 2)
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    assert chat()["reply"] == OK.reply
    main.reset_usage()                       # "another instance": no local memory, same store
    assert chat()["reply"] == OK.reply
    main.reset_usage()
    assert chat()["reply"] == main.DEMO_LIMIT_REPLY
    assert chat("198.51.100.9")["reply"] == OK.reply


def test_a_blocked_visitor_does_not_use_up_the_global_cap(monkeypatch, redis):
    monkeypatch.setattr(main, "DEMO_MAX_PER_VISITOR", 1)
    monkeypatch.setattr(main, "DEMO_DAILY_CAP", 2)
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    chat()
    for _ in range(5):
        assert chat()["reply"] == main.DEMO_LIMIT_REPLY      # spammer is blocked ...
    assert chat("198.51.100.9")["reply"] == OK.reply          # ... and others still get answers
    assert chat("192.0.2.1")["reply"] == main.DEMO_LIMIT_REPLY   # global cap 2 reached


def test_store_down_allows_requests_with_a_conservative_cap(monkeypatch, redis):
    redis.down = True
    monkeypatch.setattr(main, "DEMO_FALLBACK_CAP", 2)
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    assert chat("1.1.1.1")["reply"] == OK.reply             # fail safe: still answered
    assert chat("2.2.2.2")["reply"] == OK.reply
    assert chat("3.3.3.3")["reply"] == main.DEMO_LIMIT_REPLY  # but the local global cap is low


# ---------- model health shared through the store ----------

def test_daily_quota_flag_is_shared(monkeypatch, redis):
    calls = []
    import httpx
    import openai
    request = httpx.Request("POST", "https://x.test")
    quota = openai.RateLimitError("tokens per day (TPD) reached", body=None,
                                  response=httpx.Response(429, request=request))

    def fake_quota(provider, model, messages, **kw):
        calls.append(model)
        if model == "primary":
            raise quota
        return OK

    monkeypatch.setattr(bot, "call_model", fake_quota)
    bot.answer("بكام؟")
    assert calls == ["primary", "second"]
    bot.reset_state()                          # "another instance"
    calls.clear()
    bot.answer("بكام؟")
    assert calls == ["second"]                 # skipped from the shared flag, without calling it


def test_breaker_state_is_shared(monkeypatch, redis):
    import httpx
    import openai
    request = httpx.Request("POST", "https://x.test")
    overloaded = openai.InternalServerError("overloaded", body=None, response=httpx.Response(503, request=request))
    calls = []

    def fake(provider, model, messages, **kw):
        calls.append(model)
        if model == "primary":
            raise overloaded
        return OK

    monkeypatch.setattr(bot, "call_model", fake)
    bot.answer("1")          # primary: 503 + retry = 2 failures
    bot.reset_state()
    bot.answer("2")          # 3rd failure (count read from the store) -> breaker opens, shared
    bot.reset_state()
    calls.clear()
    _, meta = bot.answer("3")
    assert calls == ["second"]
    assert meta["attempts"][0]["result"] == "skipped_breaker"


def test_model_health_works_when_store_is_down(monkeypatch, redis):
    redis.down = True
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    result, meta = bot.answer("بكام؟")
    assert result["reply"] == OK.reply and meta["model_used"] == "primary"


# ---------- request time budget ----------

def test_request_never_runs_past_the_budget(monkeypatch, redis):
    monkeypatch.setattr(bot, "REQUEST_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(bot, "MIN_CALL_SECONDS", 0.1)
    timeouts = []

    def slow(provider, model, messages, timeout=None):
        timeouts.append(timeout)
        time.sleep(timeout)
        raise TimeoutError("slow")

    monkeypatch.setattr(bot, "call_model", slow)
    t0 = time.monotonic()
    result, meta = bot.answer("بكام؟")
    assert time.monotonic() - t0 < 1.0
    assert result["handoff"] and result["reply"] == bot.HANDOFF_MESSAGE
    assert all(t <= 0.6 for t in timeouts)                    # each call capped by what's left
    assert any(a["result"] == "skipped_budget" for a in meta["attempts"])


def test_worst_case_fits_vercel_max_duration():
    # REQUEST_BUDGET + Langfuse send + store calls must stay under maxDuration in vercel.json (60 s).
    import json
    from pathlib import Path
    config = json.loads((Path(__file__).resolve().parent.parent / "vercel.json").read_text(encoding="utf-8"))
    max_duration = config["functions"]["app/main.py"]["maxDuration"]
    store_calls = 3 * store.TIMEOUT_SECONDS
    assert bot.REQUEST_BUDGET_SECONDS + monitoring.SEND_TIMEOUT_SECONDS + store_calls < max_duration


# ---------- read-only disk ----------

def test_read_only_disk_does_not_break_chat(monkeypatch, redis, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(bot, "LOG_FILE", blocker / "requests.jsonl")   # mkdir/open will fail
    monkeypatch.setattr(bot, "call_model", lambda *a, **k: OK)
    assert chat()["reply"] == OK.reply
