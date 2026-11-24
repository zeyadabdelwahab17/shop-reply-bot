# Egyptian-Arabic Shop Reply Bot — project guide

Portfolio project for an AI Engineer job search (Egypt / Gulf / remote). Built in 5 days, 2–3 hours per day.
Do ONE day per request, then stop and wait for the user. Never start the next day on your own.

## What the bot does
A bot for small online clothing shops in Egypt. It answers customer questions (prices, sizes, stock,
shipping, returns, payment) in Egyptian Arabic.
1. It answers ONLY from `data/shop_info.json`. It never invents prices, sizes or policies.
2. If the answer is not in the file, or it is not sure, it replies with the handoff message
   (`handoff_message` in the shop file: "هحوّلك لحد من المحل يرد عليك حالًا 🙏") and flags the conversation.
3. A public page shows its measured accuracy.
Small and focused: one problem, solved well, easy to see. No extra features.

## Fixed tech stack (do not swap anything without asking)
- Python + FastAPI
- LLM: Gemini API free tier, Flash-class model. Key in `.env` (`GEMINI_API_KEY`, `GEMINI_MODEL`)
- No vector DB: the whole shop file goes into the prompt as context
- Tests: pytest. CI: GitHub Actions
- Monitoring: Langfuse Cloud free Hobby plan
- Hosting: Google Cloud Run free tier
- Simple WhatsApp-style HTML chat page served by FastAPI

## Plan
- Day 1: data + repo setup — **DONE (24 Nov 2026)**
- Day 2: the bot + chat page — **DONE (24 Nov 2026)**
- Day 3: eval script + CI accuracy gate
- Day 4: deploy + monitoring + stats page
- Day 5: README, demo video script, LinkedIn post

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
- `app/bot.py`: loads shop file once, prompt from `app/prompts/system_v1.txt` (`PROMPT_VERSION`),
  JSON output `{reply, handoff, reason}`, temperature 0.2, last 6 history messages.
  Retry: 503/429 on the main model → wait 2s, retry once → fallback model → handoff message. Never raises.
  Hard 20s limit per call (worker thread), because the SDK timeout alone let calls run 120s+.
  If handoff=true the reply always contains the exact handoff message.
  `log_request()` appends technical fields only (no message text) to `logs/requests.jsonl` (gitignored);
  Day 4 should send the same record to Langfuse from there.
- `app/main.py`: POST /chat, GET /health, GET / (`app/static/index.html`, WhatsApp-style RTL page).
- `scripts/try_bot.py` (real API, 7 questions), `scripts/fallback_report.py` (log summary).
- `tests/test_api.py`: 13 tests, Gemini mocked. 16 tests total, all passing.
- KNOWN ISSUE: `gemini-2.5-flash` returns 404 "no longer available to new users" for this key, so the
  fallback never works. User must pick another fallback model (decision pending).
- Main model latency is 4–15s per reply and sometimes over 20s (→ handoff). Not tuned yet.
