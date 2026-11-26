"""Real eval: ask the bot all questions in evals/questions.json and grade the replies deterministically.

Run:  .\\.venv\\Scripts\\python.exe evals\\run_eval.py --model groq:qwen/qwen3.8-27b --prompt v3
      optional: --limit 5   --fresh (ignore saved progress)   --min-accuracy 80   --no-mlflow

- ONE model only (--model). The fallback chain is not used, so results never mix models.
- A question is correct only if: valid JSON, handoff == should_handoff, every must_contain item matches,
  no must_not_contain item matches (see evals/grading.py).
- A failed API call (error, timeout, rate limit after retries) is "invalid": not wrong, not counted in
  accuracy, reported separately.
- Paced for Groq's free tier (8000 tokens/min) using its rate-limit headers.
- Progress is saved after every question; running the same command again resumes (invalid ones are retried).
- Output: evals/results/<timestamp>_<model>_<prompt>.json, a printed table, and an MLflow run in ./mlruns.
Exit code: 0 ok, 1 below --min-accuracy (or too many invalid), 2 daily quota used up (resume later).
"""

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import bot  # noqa: E402
from evals.grading import grade  # noqa: E402

QUESTION_SETS = {"main": ROOT / "evals" / "questions.json", "holdout": ROOT / "evals" / "holdout.json",
                 "smoke": ROOT / "evals" / "questions.json"}
# Smoke set: 10 key questions from the main set for quick CI checks (~25k Groq tokens instead of ~78k).
SMOKE_IDS = ["q01", "q07", "q11", "q19", "q21", "q24", "q26", "q27", "q28", "q31"]
#            price  stock  ship   pay    franco size   hand   catalog hand   inject


def load_questions(set_name):
    questions = json.loads(QUESTION_SETS[set_name].read_text(encoding="utf-8"))
    if set_name == "smoke":
        by_id = {q["id"]: q for q in questions}
        questions = [by_id[i] for i in SMOKE_IDS]
    return questions
RESULTS_DIR = ROOT / "evals" / "results"
MLRUNS_DIR = ROOT / "mlruns"
TRACKING_URI = "sqlite:///" + (MLRUNS_DIR / "mlflow.db").as_posix()
EXPERIMENT = "shop-reply-bot-evals"

TOKENS_PER_CALL = 3000      # ~2.4k prompt + reply; wait if fewer tokens are left this minute
MAX_TRIES = 4               # per question, for rate limits / 503 / timeouts
MIN_GRADED_SHARE = 0.8      # with --min-accuracy: fail if fewer than 80% of questions could be graded


class DailyQuotaUsedUp(Exception):
    pass


def slug(text):
    return re.sub(r"[^A-Za-z0-9.-]+", "-", text).strip("-")


def parse_seconds(value):
    """Groq reset headers look like "7.66s", "1m2.5s" or "120ms"."""
    if not value:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(value)):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    if not found:
        try:
            return float(value)
        except ValueError:
            return None
    return total


class Pacer:
    """Waits before a call if the provider says there are not enough tokens left this minute."""

    def __init__(self, fallback_wait):
        self.fallback_wait = fallback_wait   # for providers without rate-limit headers (Gemini)
        self.remaining = None
        self.reset_at = 0.0
        self.last_call = 0.0

    def wait(self):
        now = time.monotonic()
        if self.remaining is not None:
            if self.remaining < TOKENS_PER_CALL and self.reset_at > now:
                pause = self.reset_at - now + 0.5
                print(f"    (pacing: {self.remaining} tokens left this minute, waiting {pause:.1f}s)")
                time.sleep(pause)
        elif self.last_call:
            pause = self.fallback_wait - (now - self.last_call)
            if pause > 0:
                time.sleep(pause)
        self.last_call = time.monotonic()

    def update(self, headers):
        remaining = headers.get("x-ratelimit-remaining-tokens") if headers else None
        if remaining is None:
            self.remaining = None
            return
        self.remaining = int(float(remaining))
        self.reset_at = time.monotonic() + (parse_seconds(headers.get("x-ratelimit-reset-tokens")) or 60)


def call_pinned(provider, model, messages, pacer):
    """Call ONE model (no fallback). Returns (text, latency_ms). Raises after MAX_TRIES failures."""
    last = None
    for attempt in range(1, MAX_TRIES + 1):
        pacer.wait()
        t0 = time.monotonic()
        future = bot._executor.submit(bot.complete, provider, model, messages)
        try:
            text, headers, *_ = future.result(timeout=bot.CALL_TIMEOUT_SECONDS)
            pacer.update(headers)
            return text, round((time.monotonic() - t0) * 1000)
        except Exception as exc:
            if isinstance(exc, TimeoutError):   # hard limit hit (same 12 s as the live bot)
                future.cancel()
            kind = bot.error_type(exc)
            last = f"{kind}: {exc}"[:300]
            if kind == "quota_daily":
                raise DailyQuotaUsedUp(last) from None
            if kind == "429":
                response = getattr(exc, "response", None)
                retry_after = parse_seconds(response.headers.get("retry-after")) if response is not None else None
                pause = (retry_after or 20) + 1
            elif kind in ("503", "timeout"):
                pause = 3
            else:
                break   # bad request, no key, ...: retrying will not help
            print(f"    ({kind}, try {attempt}/{MAX_TRIES}, waiting {pause:.0f}s)")
            time.sleep(pause)
    raise RuntimeError(last)


MAX_QUOTA_WAIT_SECONDS = 40 * 60


def quota_wait_seconds(error):
    """Groq's daily token limit refills gradually and says "Please try again in 9m1.7s". Returns seconds,
    or None if no usable time is given (or it is longer than MAX_QUOTA_WAIT_SECONDS)."""
    m = re.search(r"try again in ([\dhms.]+)", error)
    seconds = parse_seconds(m.group(1)) if m else None
    return seconds if seconds is not None and seconds <= MAX_QUOTA_WAIT_SECONDS else None


def is_json_failure(error):
    """Groq's JSON mode returns 400 "Failed to generate JSON" when the model's output is not valid JSON."""
    return "failed to generate json" in error.lower() or "json_validate_failed" in error.lower()


def regrade(records, questions):
    """Grade stored replies again with the current questions.json (no API calls)."""
    by_id = {q["id"]: q for q in questions}
    out = []
    for r in records:
        q = by_id[r["id"]]
        if r["status"] == "invalid" and is_json_failure(r.get("error", "")):
            r = {**r, "status": "wrong", "invalid_json": True, "problems": ["invalid_json"]}
        elif "reply" in r:
            g = grade(q, {"reply": r["reply"], "handoff": r["handoff"]})
            r = {**r, "status": "correct" if g["correct"] else "wrong", "problems": g["problems"]}
        out.append(r)
    return out


def run_question(q, provider, model, system_prompt, pacer):
    record = {"id": q["id"], "category": q["category"], "question": q["question"],
              "should_handoff": q["should_handoff"]}
    messages = bot.build_messages(q["question"], [], system_prompt)
    try:
        text, ms = call_pinned(provider, model, messages, pacer)
    except DailyQuotaUsedUp:
        raise
    except Exception as exc:
        if is_json_failure(str(exc)):   # the model produced broken JSON: its fault, so it counts as wrong
            return {**record, "status": "wrong", "invalid_json": True, "error": str(exc)[:300],
                    "problems": ["invalid_json"]}
        return {**record, "status": "invalid", "error": str(exc)[:300]}
    record["latency_ms"] = ms
    try:
        result = bot.finalize(bot.parse_reply(text))
    except Exception:
        return {**record, "status": "wrong", "invalid_json": True, "raw": text[:500],
                "problems": ["invalid_json"]}
    g = grade(q, result)
    return {**record, "status": "correct" if g["correct"] else "wrong", "invalid_json": False,
            "handoff": result["handoff"], "reply": result["reply"], "reason": result["reason"],
            "problems": g["problems"]}


def category_group(category):
    return "handoff" if category.startswith("handoff_") else category


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]   # nearest-rank


def compute_metrics(records):
    graded = [r for r in records if r["status"] != "invalid"]
    correct = [r for r in graded if r["status"] == "correct"]
    latencies = [r["latency_ms"] for r in graded if "latency_ms" in r]
    per_cat = defaultdict(lambda: [0, 0])
    for r in graded:
        per_cat[category_group(r["category"])][1] += 1
        per_cat[category_group(r["category"])][0] += r["status"] == "correct"
    metrics = {
        "questions": len(records),
        "graded": len(graded),
        "invalid": len(records) - len(graded),
        "correct": len(correct),
        "accuracy": round(100 * len(correct) / len(graded), 1) if graded else 0.0,
        "false_handoffs": sum("false_handoff" in r.get("problems", []) for r in graded),
        "missed_handoffs": sum("missed_handoff" in r.get("problems", []) for r in graded),
        "invalid_json": sum(bool(r.get("invalid_json")) for r in graded),
        "latency_avg_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "latency_p95_ms": p95(latencies),
    }
    per_category = {c: {"correct": ok, "total": n, "accuracy": round(100 * ok / n, 1)}
                    for c, (ok, n) in sorted(per_cat.items())}
    return metrics, per_category


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def log_to_mlflow(run_info, metrics, per_category, results_path):
    import mlflow

    # MLflow 3 no longer accepts the plain file store, so run data goes to a SQLite file; everything
    # (database + artifacts) stays inside ./mlruns, no server needed.
    MLRUNS_DIR.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(TRACKING_URI)
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, artifact_location=(MLRUNS_DIR / "artifacts").as_uri())
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=f"{run_info['model']} {run_info['prompt_version']} {run_info.get('question_set', 'main')}") as run:
        mlflow.log_params({"model": run_info["model"], "prompt_version": run_info["prompt_version"],
                           "question_set": run_info.get("question_set", "main"),
                           "question_count": metrics["questions"], "temperature": bot.TEMPERATURE})
        mlflow.set_tags({"git_commit": run_info["git_commit"] or "unknown", "grader": "deterministic"})
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        mlflow.log_metrics({f"acc_{c}": v["accuracy"] for c, v in per_category.items()})
        mlflow.log_artifact(str(results_path))
        return run.info.run_id


def print_report(run_info, metrics, per_category, records):
    print(f"\n=== {run_info['model']} | {run_info['prompt_version']} | "
          f"{run_info.get('question_set', 'main')} set | {metrics['questions']} questions")
    rows = [("accuracy", f"{metrics['accuracy']}%  ({metrics['correct']}/{metrics['graded']} graded)"),
            ("invalid (call failed)", metrics["invalid"]),
            ("false handoffs", metrics["false_handoffs"]),
            ("missed handoffs", metrics["missed_handoffs"]),
            ("invalid JSON", metrics["invalid_json"]),
            ("latency avg / p95", f"{metrics['latency_avg_ms']} / {metrics['latency_p95_ms']} ms")]
    for name, value in rows:
        print(f"  {name:<24} {value}")
    print("  per category:")
    for c, v in per_category.items():
        print(f"    {c:<22} {v['correct']}/{v['total']}")
    failed = [r for r in records if r["status"] != "correct"]
    if failed:
        print("\n--- not correct:")
    for r in failed:
        if r["status"] == "invalid":
            print(f"[{r['id']}] INVALID: {r['error']}")
            continue
        print(f"[{r['id']}] {r['question']}\n    problems: {', '.join(r['problems'])}")
        print(f"    bot (handoff={r.get('handoff')}): {r.get('reply') or r.get('raw') or r.get('error')}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="provider:model, e.g. groq:qwen/qwen3.8-27b")
    ap.add_argument("--regrade", help="results JSON: grade its stored replies again (no API calls), "
                                      "update the file and log it to MLflow")
    ap.add_argument("--prompt", default=bot.PROMPT_VERSION, help="v2, v3 or system_v3")
    ap.add_argument("--set", choices=list(QUESTION_SETS), default="main",
                    help="main = evals/questions.json (31), holdout = evals/holdout.json (15, never tuned on), "
                         "smoke = 10 key questions of the main set (quick CI check)")
    ap.add_argument("--limit", type=int, help="only the first N questions")
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress")
    ap.add_argument("--wait", type=float, default=6, help="seconds between calls without rate-limit headers")
    ap.add_argument("--min-accuracy", type=float, help="exit 1 if accuracy (%%) is below this")
    ap.add_argument("--no-mlflow", action="store_true")
    ap.add_argument("--wait-for-quota", action="store_true",
                    help="on Groq's daily token limit, wait the time Groq names (up to 40 min) and continue")
    args = ap.parse_args()

    if args.regrade:
        path = Path(args.regrade).resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        records = regrade(data["results"],
                          load_questions(data["run"].get("question_set", "main")))
        metrics, per_category = compute_metrics(records)
        data.update(metrics=metrics, per_category=per_category, results=records)
        data["run"]["regraded_at"] = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print_report(data["run"], metrics, per_category, records)
        if not args.no_mlflow:
            print(f"MLflow run id: {log_to_mlflow(data['run'], metrics, per_category, path)}")
        return
    if not args.model:
        sys.exit("--model is required (or --regrade)")

    chain = bot.parse_chain(args.model)
    if len(chain) != 1:
        sys.exit("--model must be exactly one provider:model")
    provider, model = chain[0]
    prompt_version = args.prompt if args.prompt.startswith("system_") else f"system_{args.prompt}"
    system_prompt = bot.build_system_prompt(prompt_version)

    questions = load_questions(args.set)[: args.limit]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{slug(provider + '-' + model)}_{prompt_version}" + ("" if args.set == "main" else f"_{args.set}")
    partial = RESULTS_DIR / f"{name}.partial.jsonl"

    done = {}
    if partial.exists() and not args.fresh:
        for line in partial.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["status"] != "invalid":
                done[r["id"]] = r
        print(f"resuming: {len(done)} questions already graded ({partial.name})")
    elif partial.exists():
        partial.unlink()

    print(f"model {provider}:{model} | prompt {prompt_version} | {len(questions)} questions\n")
    pacer = Pacer(args.wait)
    records = []
    for i, q in enumerate(questions, 1):
        if q["id"] in done and done[q["id"]]["question"] == q["question"]:
            records.append(done[q["id"]])
            continue
        while True:
            try:
                r = run_question(q, provider, model, system_prompt, pacer)
                break
            except DailyQuotaUsedUp as exc:
                pause = quota_wait_seconds(str(exc))
                if not args.wait_for_quota or pause is None:
                    print(f"\nDaily quota used up ({exc}).\nProgress saved in {partial}; run the same command later.")
                    sys.exit(2)
                print(f"    (daily token quota: Groq says retry in {pause:.0f}s, waiting; progress is saved)",
                      flush=True)
                time.sleep(pause + 5)
        records.append(r)
        with open(partial, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        mark = {"correct": "ok", "wrong": "WRONG", "invalid": "INVALID"}[r["status"]]
        print(f"[{i}/{len(questions)}] {q['id']} {mark} ({r.get('latency_ms', '-')} ms)")

    metrics, per_category = compute_metrics(records)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_info = {"model": f"{provider}:{model}", "prompt_version": prompt_version, "question_set": args.set,
                "timestamp": stamp, "git_commit": git_commit(), "grader": "evals/grading.py"}
    results_path = RESULTS_DIR / f"{stamp}_{name}.json"
    results_path.write_text(json.dumps({"run": run_info, "metrics": metrics, "per_category": per_category,
                                        "results": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    partial.unlink(missing_ok=True)

    print_report(run_info, metrics, per_category, records)
    print(f"\nresults: {results_path.relative_to(ROOT)}")
    if not args.no_mlflow:
        print(f"MLflow run id: {log_to_mlflow(run_info, metrics, per_category, results_path)}")

    if args.min_accuracy is not None:
        if metrics["graded"] < MIN_GRADED_SHARE * metrics["questions"]:
            print(f"FAIL: only {metrics['graded']}/{metrics['questions']} questions could be graded")
            sys.exit(1)
        if metrics["accuracy"] < args.min_accuracy:
            print(f"FAIL: accuracy {metrics['accuracy']}% is below {args.min_accuracy}%")
            sys.exit(1)
        print(f"PASS: accuracy {metrics['accuracy']}% >= {args.min_accuracy}%")


if __name__ == "__main__":
    main()
