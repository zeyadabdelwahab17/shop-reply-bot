# Egyptian-Arabic Shop Reply Bot

## The problem
Small Egyptian online clothing shops get the same questions all day on Instagram and WhatsApp: "بكام؟", "متاح لارج؟", "الشحن كام؟".
Owners answer by hand, so customers wait for hours and many leave before they get a reply.
This bot answers in Egyptian Arabic using only the shop's own info. When it doesn't know, it hands the chat to a person instead of guessing.

## 6-day plan
- **Day 1 ✅:** data and repo setup (shop info file, 30 test questions, data tests)
- **Day 2 ✅:** the bot (FastAPI, free LLMs with a fallback chain: Groq → Gemini) and a WhatsApp-style chat page
- **Day 3:** evals, MLflow to compare prompt versions and models, CI (GitHub Actions)
- **Day 4:** deploy to Cloud Run, monitoring with Langfuse, public stats page, demo limits
- **Day 5:** handoff tickets (customer phone, Google Sheet, Telegram alerts, daily report)
- **Day 6:** full README, demo video script, LinkedIn post

The engineering journal is in [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

> The shop "Midan 13" (ميدان ١٣) and all its data are fictional.
