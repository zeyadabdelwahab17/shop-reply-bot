# Egyptian-Arabic Shop Reply Bot — project guide

Portfolio project for an AI Engineer job search (Egypt / Gulf / remote). Built in 6 days, 2–3 hours per day.
Do ONE day per request, then stop and wait for the user. Never start the next day on your own.

## What the bot does
A bot for small online clothing shops in Egypt. It answers customer questions (prices, sizes, stock,
shipping, returns, payment) in Egyptian Arabic.
1. It answers ONLY from `data/shop_info.json`. It never invents prices, sizes or policies.
2. If the answer is not in the file, or it is not sure, it replies with the handoff message
   (`handoff_message` in the shop file: "هحوّلك لحد من المحل وهيرد عليك في أقرب وقت خلال مواعيد العمل") and flags the conversation.
3. A public page shows its measured accuracy.
Small and focused: one problem, solved well, easy to see. No extra features.

## Fixed tech stack (do not swap anything without asking)
- Python + FastAPI
- LLM: FREE tiers only, called through the OpenAI-compatible chat-completions API (`openai` SDK + base_url).
  Chain in `.env` `LLM_CHAIN` (provider:model, in order): Groq `qwen/qwen3.8-27b` (approved), then
  `gemini-3.5-flash`, then `gemini-3.1-flash-lite`. Keys: `GROQ_API_KEY`, `GEMINI_API_KEY`.
- No vector DB: the whole shop file goes into the prompt as context
- Tests: pytest. CI: GitHub Actions
- Monitoring: Langfuse Cloud free Hobby plan
- Hosting: Render free web service (no card; `render.yaml`). If Render ever asks for a card: Vercel Hobby.
- Simple WhatsApp-style HTML chat page served by FastAPI

## Plan
- Day 1: data + repo setup — **DONE (24 Nov 2026)**
- Day 2: the bot + chat page — **DONE (24 Nov 2026)**
- Day 3: evals + MLflow to compare prompt versions / models + CI (GitHub Actions) — **DONE (26 Nov 2026)**
  Mocked pytest runs on every push; the real 30-question eval runs only on manual trigger
  (`workflow_dispatch`), because of the free-tier quota.
- Day 4: held-out eval + deploy to Render + Langfuse monitoring + stats page + demo limits.
- Day 5: handoff tickets:
  - ask for the customer's phone, validated in code (11 digits, starts with 010/011/012/015)
  - scripted no-LLM fallback reply when all models are down
  - private Google Sheet with columns: ticket_id, timestamp, channel, username, phone, category, priority,
    summary, conversation, status, first_contact_time
  - instant Telegram alert for high/medium priority tickets
  - daily report via scheduled GitHub Actions
  - phone numbers live only in the sheet: never in logs or Langfuse
- Day 6: README, demo video script, LinkedIn post

## Rules
- Fictional data only. The shop "Midan 13" (ميدان ١٣) is invented. No real customer data, no real phone numbers.
- Never commit `.env` or any key (`.env` is in `.gitignore`). Commit only when the user asks.
- Keep it simple. No frameworks beyond the stack above.
- Explain anything the user must do himself as short numbered steps.

## Environment (Windows)
- Project-local venv: `.venv` (packages from `requirements.txt` are installed there)
- Run tests: `.\.venv\Scripts\python.exe -m pytest -v`
- Run the app: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` → http://127.0.0.1:8000
- Every script calls `sys.stdout.reconfigure(encoding="utf-8")` so Arabic prints on Windows.

## State after Day 1
- `data/shop_info.json`: 12 products (P03 Nile Wave Tee and P09 Denim Jacket out of stock), size guide
  S–XXL (chest/length cm + height/weight ranges, bottoms waist), 3 shipping zones (60 / 75 / 90 EGP),
  free shipping over 2000, cash on delivery, open package before paying, 14-day exchange, 7-day refund,
  cap and tote not returnable, payment: cash on delivery / InstaPay / Vodafone Cash (no cards), working hours.
- `evals/questions.json`: 30 questions (q01–q20 answerable, q21–q24 tricky: Franco, typos, two-in-one,
  size advice; q25–q30 must hand off). Fields: id, question, expected_answer (list of key facts),
  category, should_handoff.
- `tests/test_data.py`: 3 data tests.
- `.env`: `GEMINI_MODEL=gemini-3.5-flash` (works), `GEMINI_FALLBACK_MODEL=gemini-2.5-flash`.

## State after Day 2
- `app/bot.py`: provider-agnostic (OpenAI-compatible API: `openai` SDK + base_url for Groq and Gemini).
  Model chain from `LLM_CHAIN`. Prompt from `app/prompts/system_v2.txt` (`PROMPT_VERSION`; v1 kept),
  shop JSON inserted compact (~2.7-3k prompt tokens per request). JSON mode `{reply, handoff, reason}`,
  temperature 0.2, last 6 history messages, minimal reasoning per model (`reasoning_params`:
  gpt-oss "low", qwen3 "none", Gemini "minimal").
  Per model: daily-quota 429 → no retry, skipped for the rest of the day; 503/timeout → retry once (1s);
  per-minute 429 and other errors → next model; 3 failures in a row → circuit breaker skips it for 60s.
  All fail → handoff message. Never raises. Hard 12s limit per call (worker thread).
  If handoff=true the reply always contains the exact handoff message.
  `log_request()` → `logs/requests.jsonl` (gitignored): provider, model, attempts, breaker events,
  limit hits, reason; no message text, no IP. Day 4 should send the same record to Langfuse from there.
- Prompt v2 vs v1: products not in the catalog → say "we don't sell it" + suggest 1–2 catalog items,
  no handoff. Handoff only for wholesale, complaints/order problems, discounts, custom requests,
  info truly missing from the file, or unsure. `reason` must not contain personal details.
- `evals/questions.json`: q27 (sneakers) is category `not_in_catalog`, should_handoff false → 5 handoff questions.
- `app/main.py`: POST /chat (reply, handoff, reason + `debug` {provider, model_used, latency_ms, fallback_used}),
  GET /health, GET / (WhatsApp-style RTL page; "debug" toggle, off by default).
  Demo limits (in memory): `DEMO_MAX_PER_VISITOR` (15/day per IP), `DEMO_DAILY_CAP` (150/day total) →
  fixed no-LLM reply "الديمو وصل للحد بتاعه النهارده، جرّب بكرة 🙏", logged as `limit_hit`.
  Day 4: check the client IP behind Cloud Run's proxy; counters are per instance.
- Scripts: `try_bot.py` (real API, 8 questions), `fallback_report.py` (log summary incl. providers,
  breaker, quota skips, limit hits), `compare_models.py` (real API, compare models on eval questions).
- Tests: 28 passing (4 data + 24 API), all model calls mocked.
- Free tiers measured 24 Nov 2026:
  - Groq: `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`: 1000 requests/day, 8000 tokens/min each → only
    ~2–3 chat messages per minute per model with the full shop file in the prompt. ~1 s per reply.
  - Groq `allam-2-7b`: unusable (4k context, JSON mode fails, loops without it).
  - Gemini: ~20 requests/day per model; `gemini-3.5-flash` counts against the `gemini-3.6-flash` quota.
    `gemini-3.1-flash-lite` works (2 s). `gemini-2.5-flash` → 404 for new users (removed).
- Primary approved: `LLM_CHAIN=groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash,gemini:gemini-3.1-flash-lite`.
- Shop data in the prompt is minified by `shop_for_prompt()` (no note, ids, English colors/zones/payment
  methods): 2653 → 2411 prompt tokens per request with qwen. Groq reported no cached tokens for qwen.
- Input for prompt v3 (Day 3) is listed in `docs/BUILD_LOG.md` (q21 unclear yes/no, q27 suggested an
  out-of-stock item, size advice check, shorter rules text). Prompt v2 is unchanged.
- Tests: 29 passing.

## State after Day 3
- `evals/questions.json`: 31 questions (q31 = prompt-injection discount, category `injection`, no handoff).
  Each has `must_contain` (strings or `{"any_of": [...]}`) and `must_not_contain`; graded by `evals/grading.py`
  (deterministic, normalizes Arabic digits/letters, ranges, currency; Latin sizes/numbers need clean edges).
- `evals/run_eval.py --model groq:qwen/qwen3.8-27b --prompt v2`: one pinned model, Groq header pacing,
  resume from `evals/results/*.partial.jsonl`, `--regrade <file>`, `--min-accuracy`. Failed call = invalid
  (left out of accuracy); Groq "Failed to generate JSON" = invalid JSON (wrong).
- MLflow: `mlruns/mlflow.db` (SQLite; MLflow 3.18 refuses the file store) + `mlruns/artifacts`, gitignored.
  `requirements-eval.txt` adds mlflow (not needed by the app or CI). UI:
  `.\.venv\Scripts\mlflow.exe ui --backend-store-uri sqlite:///mlruns/mlflow.db`
- Results 26 Nov (qwen, 31 q): v2 100% (31/31), v3 93.5% (q08 said a sold-out tee is available, q12 invalid JSON).
  v2 stays the default (`PROMPT_VERSION` env, default `system_v2`). v4 idea: explicit yes only after checking in_stock.
- CI: `.github/workflows/tests.yml` (mocked pytest, every push/PR), `eval.yml` (manual, needs repo secret
  `GROQ_API_KEY`, fails below `MIN_ACCURACY: "95"`).
- Grader audit done (phrases + negation-aware must_not_contain). Final MLflow runs: v2 `6be3122a3d46492285caee47c1e471bb`,
  v3 `62294a22c6a54b77974d73c8d11aefa7`.
- Tests: 59 passing.
