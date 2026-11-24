"""Summarize logs/requests.jsonl: errors, fallback use, breaker events, demo-limit hits, latency.

Run: .\\.venv\\Scripts\\python.exe scripts\\fallback_report.py [path\\to\\requests.jsonl]
Works with older log lines too (missing fields count as empty).
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent.parent / "logs" / "requests.jsonl"


def load_records(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def model_name(r):
    if r.get("model_used") is None:
        return None
    return f"{r['provider']}:{r['model_used']}" if r.get("provider") else r["model_used"]


def summarize(records):
    limit_hits = Counter(r["limit_hit"] for r in records if r.get("limit_hit"))
    llm = [r for r in records if not r.get("limit_hit")]  # requests that reached the model chain
    total = len(llm)
    errors = Counter(r["error_type"] for r in llm if r.get("error_type"))
    fallback = sum(1 for r in llm if r.get("fallback_used"))
    failed = sum(1 for r in llm if r.get("model_used") is None)
    recovered_by_retry = sum(
        1 for r in llm
        if r.get("retries") and not r.get("fallback_used") and r.get("model_used") is not None
    )
    answered_by = Counter(model_name(r) for r in llm if model_name(r))
    events = [e for r in llm for e in r.get("breaker_events", [])]
    skipped = Counter(a["result"] for r in llm for a in r.get("attempts", []) if a["result"].startswith("skipped"))
    attempt_errors = Counter(
        (a["model"], a["result"]) for r in llm for a in r.get("attempts", [])
        if a["result"] != "ok" and not a["result"].startswith("skipped")
    )
    latencies = defaultdict(list)
    for r in llm:
        latencies[model_name(r) or "none (all failed)"].append(r["latency_ms"])
    return {
        "total": total,
        "main_errors": dict(errors),
        "main_errors_total": sum(errors.values()),
        "recovered_by_retry": recovered_by_retry,
        "fallback_used": fallback,
        "fallback_pct": round(100 * fallback / total, 1) if total else 0.0,
        "failed_completely": failed,
        "answered_by": dict(answered_by),
        "errors_by_model": {f"{m} {k}": n for (m, k), n in attempt_errors.items()},
        "breaker_opens": sum(1 for e in events if e.startswith("breaker_open")),
        "quota_skips": sum(1 for e in events if e.startswith("quota_skip_today")),
        "skipped_attempts": dict(skipped),
        "limit_hits": dict(limit_hits),
        "avg_latency_ms": {m: round(sum(v) / len(v)) for m, v in latencies.items()},
    }


def print_report(s):
    print(f"Requests to the model chain:    {s['total']}")
    print(f"First error per request:        {s['main_errors_total']}")
    for kind, n in sorted(s["main_errors"].items()):
        print(f"  - {kind}: {n}")
    print(f"Recovered by retry (same model): {s['recovered_by_retry']}")
    print(f"Fallback model used:            {s['fallback_used']} ({s['fallback_pct']}%)")
    print(f"Failed completely (handoff):    {s['failed_completely']}")
    print("Answered by:")
    for model, n in sorted(s["answered_by"].items()):
        print(f"  - {model}: {n}")
    if s["errors_by_model"]:
        print("Failed attempts by model:")
        for k, n in sorted(s["errors_by_model"].items()):
            print(f"  - {k}: {n}")
    print(f"Circuit breaker opened:         {s['breaker_opens']}")
    print(f"Daily quota used up (events):   {s['quota_skips']}")
    print(f"Attempts skipped:               {sum(s['skipped_attempts'].values())} {s['skipped_attempts'] or ''}")
    print(f"Demo limit hits:                {sum(s['limit_hits'].values())} {s['limit_hits'] or ''}")
    print("Average latency per model:")
    for model, ms in sorted(s["avg_latency_ms"].items()):
        print(f"  - {model}: {ms} ms")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    if not path.exists():
        print(f"No log file yet: {path}")
        return
    print_report(summarize(load_records(path)))


if __name__ == "__main__":
    main()
