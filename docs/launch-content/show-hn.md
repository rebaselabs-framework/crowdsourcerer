# Show HN: CrowdSorcerer -- One API for 10 AI task types + a gamified human workforce

I built a unified task API that routes work to the right AI or human worker.

**The problem:** Building AI-powered features means integrating 5-10 different APIs -- web scraping, entity enrichment, document parsing, code execution, LLM routing, etc. Each has its own auth, schema, error handling, and billing.

**CrowdSorcerer:** One REST API, one auth token, one credit system. Submit a task with a type and input, we route it to the right worker and return structured results.

## 10 AI task types:

- Web Research (scrape + summarize any URL)
- Entity Lookup (enrich companies/people)
- Document Parse (PDFs, Word docs, images)
- Data Transform (CSV/JSON, clean, reshape)
- LLM Generate (Claude, GPT-4, Gemini via router)
- Screenshot (full-page captures)
- Audio Transcribe (with speaker diarization)
- PII Detect (30+ entity types, masking)
- Code Execute (Python/JS/Bash sandbox)
- Web Intel (competitive intelligence)

Plus 8 human task types for labeling, moderation, and QA.

## The worker side: Duolingo meets MTurk

Workers earn credits by completing human tasks. But instead of the MTurk model (grind for pennies), we use intrinsic motivation: daily streaks, weekly leagues, quests, badges, skill trees. Think Duolingo but for data work.

## Tech:

- FastAPI backend, Astro 5 SSR frontend
- TypeScript + Python SDKs
- HMAC-signed webhooks
- Task pipelines (chain AI + human steps)
- Credits system ($1 = 100 credits, cheapest task = 1 credit)

## Try it:

https://crowdsourcerer.rebaselabs.online

100 free credits on signup, no credit card. Sign up, grab an API key, submit a task.

SDK: `npm install @crowdsourcerer/sdk`

Docs: https://crowdsourcerer.rebaselabs.online/docs

Happy to answer questions about the architecture, the gamification design, or the AI task routing.
