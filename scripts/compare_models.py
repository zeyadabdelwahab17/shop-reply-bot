"""Compare models on a few eval questions with the current system prompt (REAL API calls).

Run: .\\.venv\\Scripts\\python.exe scripts\\compare_models.py groq:allam-2-7b groq:openai/gpt-oss-120b
Optional: --questions q01,q12 --wait 20

Prints each raw reply (for judging correctness and Arabic quality by hand), whether it was valid JSON,
latency, and the free-tier limits from the response headers.
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import bot  # noqa: E402

LIMIT_HEADERS = {
    "x-ratelimit-limit-requests": "requests/day",
    "x-ratelimit-remaining-requests": "requests left today",
    "x-ratelimit-limit-tokens": "tokens/min",
}


def run_one(provider, model, question):
    t0 = time.monotonic()
    try:
        text, headers = bot.complete(provider, model, bot.build_messages(question, []))
    except Exception as exc:
        return {"ok": False, "error": f"{bot.error_type(exc)}: {exc}"[:300], "ms": _ms(t0)}
    out = {"ok": True, "ms": _ms(t0), "raw": text,
           "limits": {label: headers.get(h) for h, label in LIMIT_HEADERS.items() if headers.get(h)}}
    try:
        out["parsed"] = bot.parse_reply(text).model_dump()
        out["valid_json"] = True
    except Exception:
        out["valid_json"] = False
    return out


def _ms(t0):
    return round((time.monotonic() - t0) * 1000)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="provider:model")
    ap.add_argument("--questions", default="q01,q12,q21,q24,q27")
    ap.add_argument("--wait", type=float, default=20, help="seconds between calls (free-tier tokens/min)")
    args = ap.parse_args()

    questions = {q["id"]: q for q in json.loads((ROOT / "evals" / "questions.json").read_text(encoding="utf-8"))}
    models = [bot.parse_chain(m)[0] for m in args.models]
    results = {f"{p}:{m}": [] for p, m in models}
    print(f"prompt: {bot.PROMPT_VERSION}\n")

    first = True
    for qid in args.questions.split(","):
        q = questions[qid]
        print(f"=== [{qid}] {q['question']}\n    expected: {' / '.join(q['expected_answer'])}"
              f" | handoff expected: {q['should_handoff']}")
        for provider, model in models:  # round-robin, so each model gets time between its calls
            if not first:
                time.sleep(args.wait)
            first = False
            r = run_one(provider, model, q["question"])
            results[f"{provider}:{model}"].append(r)
            if not r["ok"]:
                print(f"  {model}: ERROR ({r['ms']} ms) {r['error']}")
            elif r["valid_json"]:
                p = r["parsed"]
                print(f"  {model} ({r['ms']} ms) handoff={p['handoff']}: {p['reply']}")
            else:
                print(f"  {model} ({r['ms']} ms) INVALID JSON: {r['raw'][:300]!r}")
        print()

    print("=== summary")
    for key, rs in results.items():
        ok = [r for r in rs if r["ok"]]
        valid = sum(1 for r in ok if r.get("valid_json"))
        avg = round(sum(r["ms"] for r in ok) / len(ok)) if ok else None
        limits = next((r["limits"] for r in reversed(ok) if r.get("limits")), {})
        print(f"{key}: answered {len(ok)}/{len(rs)}, valid JSON {valid}/{len(rs)}, avg latency {avg} ms, limits {limits}")


if __name__ == "__main__":
    main()
