# @crowdsourcerer/sdk

TypeScript client for the CrowdSorcerer API — post human tasks, get back typed JSON results.

## Install

```bash
npm install @crowdsourcerer/sdk
```

## Quick Start

```ts
import { CrowdSorcerer } from "@crowdsourcerer/sdk";

const client = new CrowdSorcerer({ apiKey: "csk_..." });

// Submit a labeling task, wait until the worker(s) finish
const task = await client.runTask({
  type: "label_text",
  input: {
    text: "The new iPhone is great!",
    categories: ["positive", "negative", "neutral"],
    question: "What is the sentiment?",
  },
  assignments_required: 3, // 3 workers, majority-vote consensus
});

console.log(task.output?.summary); // → "positive (3/3 workers agree)"
```

## Configuration

```ts
const client = new CrowdSorcerer({
  apiKey: "csk_...",                                    // required
  baseUrl: "https://crowdsourcerer.rebaselabs.online", // default
  timeout: 30_000,                                     // ms, default
  maxRetries: 3,                                       // default
});
```

## Task Types

`POST /v1/tasks` accepts the 8 human task types below. The backend also
has 6 AI primitives (`llm_generate`, `data_transform`, `pii_detect`,
`document_parse`, `code_execute`, `web_research`) but those are
**pipeline-only** — they run as steps inside `/v1/pipelines`, not as
standalone submissions. The SDK's `submitTask` / `runTask` typecheck
against `HumanTaskType` to prevent a 422 at submit time.

| Type | Base credits | Description |
|------|--------------|-------------|
| `label_image` | 3 | Bounding boxes, segmentation, classification on an image |
| `label_text` | 2 | Sentiment, categories, spam detection on text |
| `rate_quality` | 2 | Score content on a 1–5 (or custom) scale |
| `verify_fact` | 3 | Check a claim against sources |
| `moderate_content` | 2 | Approve, reject, or escalate user-submitted content |
| `compare_rank` | 2 | Pick A vs B (or rank N) on any criterion |
| `answer_question` | 4 | Open-ended Q&A with optional context |
| `transcription_review` | 5 | Correct an AI-generated transcript |

Every type carries a strict input schema and a matching worker-response
schema. Import `TASK_METADATA` from `@crowdsourcerer/types` for the
canonical id/label/icon/description/base-credits record.

## Usage Patterns

### Synchronous (block until complete)

`runTask()` submits a task and polls until it finishes. Workers
typically claim and submit within the SLA you set (default 30 min).
Default poll timeout is 5 minutes — pass `timeoutMs` to raise it.

```ts
const task = await client.runTask(
  {
    type: "moderate_content",
    input: {
      content: "User-submitted post goes here",
      content_type: "text",
      policy_context: "Flag harmful or off-topic content",
    },
    assignments_required: 1,
    claim_timeout_minutes: 60,
  },
  { pollIntervalMs: 5_000, timeoutMs: 15 * 60_000 },
);

console.log(task.status); // "completed" | "failed"
console.log(task.output);
```

### Asynchronous (fire and forget)

Use `submitTask()` to enqueue work without blocking, then pull results
via webhook or polling.

```ts
const { task_id } = await client.submitTask({
  type: "verify_fact",
  input: {
    claim: "The Eiffel Tower is 330 metres tall",
    context: "Wikipedia says 330m including antennas, 300m structural",
  },
  webhook_url: "https://yourapp.com/hooks/crowdsourcerer",
});

// ... later
const task = await client.getTask(task_id);
if (task.status === "completed") {
  console.log(task.output);
}
```

### Cancellation

```ts
await client.cancelTask(taskId);
```

### Consensus across multiple workers

Any human task type accepts `assignments_required` (1–20) plus a
`consensus_strategy`. The task stays open until enough workers submit,
then the consensus strategy decides how the result is computed.

```ts
const task = await client.runTask({
  type: "rate_quality",
  input: {
    content: "<AI-generated summary>",
    criteria: "Rate factual accuracy 1–5",
  },
  assignments_required: 5,
  consensus_strategy: "majority_vote", // or "any_first" | "unanimous" | "requester_review"
  min_skill_level: 3,                  // only workers at proficiency ≥3
});
```

## Error Handling

All errors extend `CrowdSorcererError` with `status`, `code`, and `requestId` fields.

```ts
import {
  CrowdSorcerer,
  AuthError,
  RateLimitError,
  InsufficientCreditsError,
  CrowdSorcererError,
} from "@crowdsourcerer/sdk";

try {
  const task = await client.runTask({
    type: "label_text",
    input: { text: "...", categories: ["a", "b"], question: "?" },
  });
} catch (err) {
  if (err instanceof AuthError) {
    // 401 — bad or missing API key
  } else if (err instanceof RateLimitError) {
    // 429 — back off and retry
    console.error(`Rate limited. Retry after ${err.retryAfter}s`);
  } else if (err instanceof InsufficientCreditsError) {
    // 402 — not enough credits
    console.error(err.message);
  } else if (err instanceof CrowdSorcererError) {
    // Any other API error
    console.error(`${err.code} (${err.status}): ${err.message}`);
    console.error(`Request ID: ${err.requestId}`);
  }
}
```

## API Reference

### Tasks

| Method | Description |
|--------|-------------|
| `submitTask(req)` | Submit a task, return immediately with `{ task_id }` |
| `getTask(taskId)` | Get task by ID (poll for status) |
| `runTask(req, opts?)` | Submit + poll until complete or failed |
| `listTasks(params?)` | List tasks with optional `status`, `type`, `page`, `page_size` filters |
| `cancelTask(taskId)` | Cancel a pending or queued task |

### Credits

| Method | Description |
|--------|-------------|
| `getCredits()` | Get current credit balance |
| `listTransactions(params?)` | List credit transactions (paginated) |

### API Keys

| Method | Description |
|--------|-------------|
| `listApiKeys()` | List all API keys |
| `createApiKey(req)` | Create a new API key |
| `deleteApiKey(keyId)` | Delete an API key |

### User

| Method | Description |
|--------|-------------|
| `getMe()` | Get authenticated user profile |
| `getQuota()` | Get current usage quota status |

### Template Marketplace

| Method | Description |
|--------|-------------|
| `listTemplates(params?)` | Browse task templates (filterable, sortable) |
| `getTemplate(id)` | Get template details |
| `createTemplate(req)` | Publish a new template |
| `useTemplate(id)` | Use a template (creates a task from it) |
| `rateTemplate(id, rating)` | Rate a template |
| `listTemplateCategories()` | List template categories with counts |

### Worker Marketplace

| Method | Description |
|--------|-------------|
| `listMarketplaceTasks(params?)` | Browse open human tasks |
| `getPersonalisedFeed(params?)` | Skill-ranked task feed for the authenticated worker |

### Webhooks

| Method | Description |
|--------|-------------|
| `listWebhookEvents()` | List supported webhook event types |
| `getWebhookStats()` | Webhook delivery stats |
| `listWebhookLogs(params?)` | Webhook delivery logs (filterable) |

### Webhook Verification

Verify incoming webhook signatures to ensure they're from CrowdSorcerer:

```ts
import { verifyWebhook } from "@crowdsourcerer/sdk";

app.post("/webhook", (req, res) => {
  const sig = req.headers["x-crowdsorcerer-signature"] as string;
  if (!verifyWebhook(req.body, process.env.WEBHOOK_SECRET!, sig)) {
    return res.status(401).send("Invalid signature");
  }
  // Handle the event
  const { task_id, event } = JSON.parse(req.body);
  res.sendStatus(200);
});
```

Edge / browser runtimes should use `verifyWebhookAsync` (Web Crypto)
instead of `verifyWebhook` (Node `crypto`):

```ts
import { verifyWebhookAsync } from "@crowdsourcerer/sdk";

const ok = await verifyWebhookAsync(payload, secret, sigHeader);
```

Both accept an options object:

```ts
verifyWebhook(payload, secret, sigHeader, {
  toleranceSec: 300, // Reject deliveries older than 5 min (default)
});
```

## Full Documentation

[https://crowdsourcerer.rebaselabs.online/docs](https://crowdsourcerer.rebaselabs.online/docs)

## License

MIT
