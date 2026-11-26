"""Monitoring, /stats and the real visitor IP. No network: Langfuse calls are mocked or disabled."""

import json

import pytest
from fastapi.testclient import TestClient

from app import bot, main, monitoring
from app.main import app

client = TestClient(app)
REAL_SEND_TRACE = monitoring.send_trace   # kept before the fixture replaces it
OK =bot.BotReply(reply="الهودي بـ 1250 جنيه", handoff=False, reason="price of P05")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "LOG_FILE", tmp_path / "requests.jsonl")
    monkeypatch.setattr(bot, "CHAIN", [("groq", "primary")])
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    sent = []
    monkeypatch.setattr(monitoring, "send_trace", sent.append)
    bot.reset_state()
    main.reset_usage()
    monitoring.reset_local()
    yield sent
    main.reset_usage()
    monitoring.reset_local()


def reply_with_usage():
    r = OK.model_copy()
    r._usage = {"input": 2411, "output": 40, "total": 2451}
    return r


def test_chat_sends_one_trace_with_metadata_and_tokens(monkeypatch, isolated):
    monkeypatch.setattr(bot, "call_model", lambda p, m, msgs, **kw: reply_with_usage())
    assert client.post("/chat", json={"message": "الهودي بكام؟ رقمي 01012345678"}).status_code == 200
    assert len(isolated) == 1
    record = isolated[0]
    assert record["model_used"] == "primary" and record["prompt_version"] == bot.PROMPT_VERSION
    assert record["usage"] == {"input": 2411, "output": 40, "total": 2451}
    # No message text (and so no phone number) anywhere in what goes to Langfuse.
    body = json.dumps(monitoring.build_otlp(record), ensure_ascii=False)
    assert "01012345678" not in body and "الهودي بكام" not in body


def spans_of(body):
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attrs(span):
    out = {}
    for a in span["attributes"]:
        v = a["value"]
        out[a["key"]] = [x["stringValue"] for x in v["arrayValue"]["values"]] if "arrayValue" in v else v["stringValue"]
    return out


def test_otlp_body_shape():
    record = {"prompt_version": "system_v2", "provider": "groq", "model_used": "qwen", "fallback_used": True,
              "latency_ms": 800, "handoff": False, "breaker_events": [], "usage": {"input": 10, "output": 2, "total": 12}}
    root, gen = spans_of(monitoring.build_otlp(record))
    a, g = attrs(root), attrs(gen)
    assert root["name"] == "chat" and gen["parentSpanId"] == root["spanId"] and gen["traceId"] == root["traceId"]
    assert len(root["traceId"]) == 32 and len(root["spanId"]) == 16
    assert int(root["endTimeUnixNano"]) - int(root["startTimeUnixNano"]) == 800_000_000
    assert a["langfuse.trace.name"] == "chat" and "fallback" in a["langfuse.trace.tags"]
    assert a["langfuse.observation.metadata.fallback_used"] == "true"
    assert a["langfuse.observation.metadata.latency_ms"] == "800"
    assert a["langfuse.environment"] == monitoring.APP_ENV
    # Trace-level attributes are repeated on every span.
    assert g["langfuse.trace.tags"] == a["langfuse.trace.tags"]
    assert g["langfuse.observation.type"] == "generation" and g["langfuse.observation.model.name"] == "qwen"
    assert json.loads(g["langfuse.observation.usage_details"]) == {"input": 10, "output": 2, "total": 12}
    # A demo-limit hit has no model call, so no generation span.
    assert len(spans_of(monitoring.build_otlp({"limit_hit": "visitor", "latency_ms": 0}))) == 1


def test_summarize_reads_string_metadata():
    metas = [{"fallback_used": "true", "handoff": "false", "latency_ms": "600"},
             {"fallback_used": False, "handoff": True, "latency_ms": 400}]
    assert monitoring.summarize(metas)["fallback_pct"] == 50.0
    assert monitoring.summarize(metas)["avg_latency_ms"] == 500


def test_langfuse_stats_follows_the_cursor(monkeypatch):
    pages = [{"data": [{"metadata": {"handoff": True, "latency_ms": 100}, "startTime": "2026-11-26T09:00:00Z"}],
              "meta": {"cursor": "next"}},
             {"data": [{"metadata": {"limit_hit": "global"}, "startTime": "2026-11-26T08:00:00Z"}], "meta": {}}]
    seen = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params):
            seen.append((url, dict(params)))
            return FakeResponse(pages[len(seen) - 1])

    monkeypatch.setattr(monitoring.httpx, "Client", FakeClient)
    stats = monitoring._langfuse_stats(("pk", "sk", "https://lf.test"))
    assert seen[0][0] == "https://lf.test/api/public/v2/observations"
    assert seen[0][1]["isRootObservation"] == "true" and "cursor" not in seen[0][1]
    assert seen[1][1]["cursor"] == "next"
    assert stats["requests"] == 2 and stats["limit_hits"] == 1 and stats["since"] == "2026-11-26T08:00:00Z"


class FakeSender:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise monitoring.httpx.ConnectError("down")
        return monitoring.httpx.Response(200, request=monitoring.httpx.Request("POST", url))


def test_send_trace_does_nothing_without_keys(monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(monitoring, "_sender", lambda: sender)
    REAL_SEND_TRACE({"model_used": "x"})
    assert not sender.calls


def test_send_trace_posts_otlp_with_v4_header(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    sender = FakeSender()
    monkeypatch.setattr(monitoring, "_sender", lambda: sender)
    REAL_SEND_TRACE({"model_used": "x", "latency_ms": 5})
    url, kw = sender.calls[0]
    assert url.endswith("/api/public/otel/v1/traces")
    assert kw["headers"]["x-langfuse-ingestion-version"] == "4" and "resourceSpans" in kw["json"]


def test_send_trace_never_raises(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(monitoring, "_sender", lambda: FakeSender(fail=True))
    REAL_SEND_TRACE({"model_used": "x", "latency_ms": 5})   # logged, not raised


def test_summarize_counts_limit_hits_separately():
    metas = [{"fallback_used": True, "handoff": False, "latency_ms": 1000},
             {"fallback_used": False, "handoff": True, "latency_ms": 500},
             {"limit_hit": "global"}]
    assert monitoring.summarize(metas) == {"requests": 3, "fallback_pct": 50.0, "handoff_pct": 50.0,
                                           "avg_latency_ms": 750, "limit_hits": 1}


def test_stats_from_memory_without_langfuse(monkeypatch):
    monkeypatch.setattr(bot, "call_model", lambda p, m, msgs, **kw: OK)
    client.post("/chat", json={"message": "الهودي بكام؟"})
    live = client.get("/stats.json").json()["live"]
    assert live["source"] == "memory" and live["requests"] == 1 and "instance" in live["note"]


def test_stats_page_is_served():
    r = client.get("/stats")
    assert r.status_code == 200 and "holdout" in r.text


def test_latest_evals_mean_and_min(tmp_path):
    def write(name, prompt, acc, qset="main"):
        (tmp_path / name).write_text(json.dumps({
            "run": {"prompt_version": prompt, "question_set": qset, "timestamp": name[:15], "model": "groq:qwen"},
            "metrics": {"accuracy": acc, "correct": 1, "graded": 1, "questions": 31}}), encoding="utf-8")
    write("20261126-010000_a.json", "system_v2", 90.0)     # oldest: not in the latest 3
    write("20261126-020000_b.json", "system_v2", 100.0)
    write("20261126-030000_c.json", "system_v2", 96.8)
    write("20261126-040000_d.json", "system_v2", 93.5)
    write("20261126-050000_e.json", "system_v3", 50.0)     # other prompt: ignored
    write("20261126-060000_h.json", "system_v2", 80.0, "holdout")
    ev = main.latest_evals(tmp_path, "system_v2")
    assert ev["main"]["runs"] == 3 and ev["main"]["min"] == 93.5 and ev["main"]["mean"] == 96.8
    assert ev["holdout"]["accuracy"] == 80.0


def test_faked_x_forwarded_for_does_not_dodge_the_visitor_limit(monkeypatch):
    # The client fakes the left entries; the proxy appends the real address (the last entry).
    monkeypatch.setattr(main, "DEMO_MAX_PER_VISITOR", 1)
    monkeypatch.setattr(bot, "call_model", lambda p, m, msgs, **kw: OK)
    first = {"X-Forwarded-For": "203.0.113.7"}
    faked = {"X-Forwarded-For": "1.2.3.4, 203.0.113.7"}
    other = {"X-Forwarded-For": "1.2.3.4, 198.51.100.9"}
    assert client.post("/chat", json={"message": "1"}, headers=first).json()["reply"] == OK.reply
    assert client.post("/chat", json={"message": "2"}, headers=faked).json()["reply"] == main.DEMO_LIMIT_REPLY
    assert client.post("/chat", json={"message": "3"}, headers=other).json()["reply"] == OK.reply


def test_trusted_proxy_hops(monkeypatch):
    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff}
            self.client = None
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 2)   # e.g. CDN + load balancer
    assert main.visitor_id(Req("1.2.3.4, 203.0.113.7, 10.0.0.1")) == "203.0.113.7"
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 1)
    assert main.visitor_id(Req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"


def test_on_vercel_the_ip_comes_from_x_real_ip(monkeypatch):
    # Vercel overwrites X-Forwarded-For / X-Real-IP with the real client IP (no spoofing).
    monkeypatch.setattr(main, "ON_VERCEL", True)

    class Req:
        headers = {"x-real-ip": "203.0.113.7", "x-forwarded-for": "203.0.113.7"}
        client = None
    assert main.visitor_id(Req()) == "203.0.113.7"


def test_debug_ip_endpoint_is_gone():
    assert client.get("/debug/ip").status_code == 404


def test_empty_langfuse_keys_mean_monitoring_off(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "  ")
    assert not monitoring.enabled()
    sender = FakeSender()
    monkeypatch.setattr(monitoring, "_sender", lambda: sender)
    REAL_SEND_TRACE({"model_used": "x"})
    assert not sender.calls and monitoring.live_stats()["source"] == "memory"
