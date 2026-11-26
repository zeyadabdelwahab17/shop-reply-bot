"""Every test runs without real services: no Langfuse, no Upstash, not "on Vercel", even if the local
.env has keys (app/bot.py loads .env at import)."""

import pytest

from app import bot, monitoring, store

EXTERNAL_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
                "VERCEL")


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    for key in EXTERNAL_ENV:
        monkeypatch.delenv(key, raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach a real external service")

    monkeypatch.setattr(store, "_http", no_network)
    monkeypatch.setattr(monitoring, "_sender", no_network)
    bot.reset_state()
    yield
    bot.reset_state()
