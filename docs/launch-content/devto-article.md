---
title: "I built a unified API for 10 AI task types (and a gamified worker marketplace)"
published: false
description: "One API for web scraping, entity enrichment, document parsing, LLM routing, code execution, and more. Plus a Duolingo-style workforce for data labeling."
tags: api, ai, opensource, webdev
cover_image: 
---

## The Problem

Every AI-powered product I've built needed the same set of capabilities:

- Scrape a webpage and summarize it
- Enrich a company name into structured data
- Parse a PDF into JSON
- Run some code in a sandbox
- Call an LLM with the right prompt
- Have a human verify the output

Each capability meant a new API integration. New auth flow. New error handling. New billing dashboard. By the time I had 5 integrations, half my codebase was glue code.

## The Solution: One API, One Token, One Credit System

[CrowdSorcerer](https://crowdsourcerer.rebaselabs.online) is a task broker. You submit a task with a `type` and `input`, and we route it to the right worker -- AI for automation, human for judgment.

```bash
curl -X POST https://crowdsourcerer.rebaselabs.online/v1/tasks \
  -H "Authorization: Bearer csk_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "web_research",
    "input": {
      "url": "https://news.ycombinator.com",
      "instruction": "List the top 5 stories with their scores"
    }
  }'
```

Response:

```json
{
  "task_id": "task_8f3a2c1d",
  "status": "queued",
  "estimated_credits": 10
}
```

Poll for the result (or set up a webhook):

```json
{
  "status": "completed",
  "output": {
    "summary": "Top 5 HN stories: ...",
    "sources": ["https://news.ycombinator.com"]
  },
  "credits_used": 10,
  "duration_ms": 3200
}
```

## 10 AI Task Types

| Task | Credits | What it does |
|------|---------|-------------|
| Web Research | 10 | Scrape + summarize any URL |
| Entity Lookup | 5 | Enrich companies or people |
| Document Parse | 3 | Extract text/tables from PDFs |
| Data Transform | 2 | Reshape data between formats |
| LLM Generate | 1 | Route to the best LLM |
| Screenshot | 2 | Full-page webpage captures |
| Audio Transcribe | 8 | Speech-to-text with diarization |
| PII Detect | 2 | Find and mask personal data |
| Code Execute | 3 | Run code in a sandbox |
| Web Intel | 5 | Competitive intelligence |

$1 = 100 credits. Cheapest task costs 1 credit (1 cent).

## The TypeScript SDK

```ts
import { CrowdSorcerer } from "@crowdsourcerer/sdk";

const client = new CrowdSorcerer({ apiKey: "csk_..." });

// One-liner: submit + wait for result
const task = await client.webResearch({
  url: "https://example.com",
  instruction: "Extract the company description and pricing",
});

console.log(task.output.summary);
```

Every task type has a typed helper with autocomplete. Or use the lower-level `submitTask()` + `getTask()` for async workflows.

```bash
npm install @crowdsourcerer/sdk
```

## The Worker Side: Duolingo for Data Work

Here's where it gets interesting. CrowdSorcerer isn't just an API -- it's a two-sided marketplace.

Human task types include image labeling, text classification, quality ratings, fact verification, content moderation, and more. But instead of the typical MTurk model (anonymous gig workers grinding for $2/hour), we use intrinsic motivation:

- **Daily streaks** -- complete at least one task per day to keep your streak alive
- **Weekly leagues** -- compete with other workers for promotion (Bronze → Silver → Gold → Diamond)
- **Quests** -- weekly challenges like "Complete 10 tasks" or "Maintain 95% accuracy"
- **Badges** -- unlock achievements for milestones
- **Skill trees** -- declare expertise, get matched to relevant tasks

The hypothesis: workers who enjoy what they do produce better data. Games-researcher literature backs this up -- intrinsic motivation (Duolingo model) sustains engagement better than extrinsic (pure pay).

## Tech Stack

| Layer | Tech |
|-------|------|
| API | FastAPI, asyncpg, Pydantic |
| Frontend | Astro 5 SSR, Tailwind |
| Database | PostgreSQL |
| Auth | JWT + refresh tokens, 2FA (TOTP), API keys |
| Payments | Credits system (Stripe for fiat) |
| SDKs | TypeScript, Python |
| Deploy | Docker Compose on Coolify (Hetzner) |

The API has 335 endpoints, 1262 tests, and 62 database migrations. Security includes HMAC-signed webhooks, SSRF protection, rate limiting, and DB-level credit constraints.

## Try It

**Sign up:** [crowdsourcerer.rebaselabs.online/register](https://crowdsourcerer.rebaselabs.online/register) -- 100 free credits, no credit card.

**Docs:** [crowdsourcerer.rebaselabs.online/docs](https://crowdsourcerer.rebaselabs.online/docs)

**SDK:** `npm install @crowdsourcerer/sdk`

**API Sandbox:** [crowdsourcerer.rebaselabs.online/docs/sandbox](https://crowdsourcerer.rebaselabs.online/docs/sandbox) -- try every task type interactively.

I'm building this as a solo dev. Would love feedback on:

1. Which task types are most useful for your workflow?
2. Would the gamified worker model motivate you to contribute?
3. What's missing that would make you actually integrate this?

---

*Built by [RebaseLabs](https://rebaselabs.online). Follow the build journey at [rebaselabs.online](https://rebaselabs.online).*
