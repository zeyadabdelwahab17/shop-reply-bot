"""Summarize logs/requests.jsonl: main-model errors, fallback use, complete failures, latency.

Run: .\\.venv\\Scripts\\python.exe scripts\\fallback_report.py [path\\to\\requests.jsonl]
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


def summarize(records):
    total = len(records)
    errors = Counter(r["error_type"] for r in records if r.get("error_type"))
    fallback = sum(1 for r in records if r.get("fallback_used"))
    failed = sum(1 for r in records if r.get("model_used") is None)
    recovered_by_retry = sum(
        1 for r in records
        if r.get("retries") and not r.get("fallback_used") and r.get("model_used") is not None
    )
    latencies = defaultdict(list)
    for r in records:
        latencies[r.get("model_used") or "none (all failed)"].append(r["latency_ms"])
    return {
        "total": total,
        "main_errors": dict(errors),
        "main_errors_total": sum(errors.values()),
        "recovered_by_retry": recovered_by_retry,
        "fallback_used": fallback,
        "fallback_pct": round(100 * fallback / total, 1) if total else 0.0,
        "failed_completely": failed,
        "avg_latency_ms": {m: round(sum(v) / len(v)) for m, v in latencies.items()},
    }


def print_report(s):
    print(f"Total requests:                 {s['total']}")
    print(f"Main model errors:              {s['main_errors_total']}")
    for kind, n in sorted(s["main_errors"].items()):
        print(f"  - {kind}: {n}")
    print(f"Recovered by retry (same model): {s['recovered_by_retry']}")
    print(f"Fallback model used:            {s['fallback_used']} ({s['fallback_pct']}%)")
    print(f"Failed completely (handoff):    {s['failed_completely']}")
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
