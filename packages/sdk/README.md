# @crowdsourcerer/sdk

TypeScript client for the CrowdSorcerer API -- a unified task API for AI and human workforce tasks.

## Install

```bash
npm install @crowdsourcerer/sdk
```

## Quick Start

```ts
import { CrowdSorcerer } from "@crowdsourcerer/sdk";

const client = new CrowdSorcerer({ apiKey: "csk_..." });
const task = await client.webResearch({
  url: "https://example.com",
  instruction: "What does this company do?",
});
console.log(task.output);
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

### AI Tasks

| Type | Helper method | Credits | Description |
|------|--------------|---------|-------------|
| `web_research` | `webResearch()` | 10 | Scrape and summarize any URL |
| `entity_lookup` | `entityLookup()` | 5 | Enrich companies or people |
| `document_parse` | `documentParse()` | 3 | Extract structured data from PDFs/docs |
| `data_transform` | `dataTransform()` | 2 | Transform data between formats |
| `llm_generate` | `llmGenerate()` | 1 | Generate text via LLM |
| `screenshot` | `screenshot()` | 2 | Capture webpage screenshots |
| `audio_transcribe` | `audioTranscribe()` | 8 | Speech to text |
| `pii_detect` | `piiDetect()` | 2 | Detect personally identifiable information |
| `code_execute` | `codeExecute()` | 3 | Run code in a sandbox |
| `web_intel` | `webIntel()` | 5 | Competitive and market intelligence |

### Human Tasks

8 human task types are available for labeling, annotation, QA, and other tasks that require human judgment. Submit them via `submitTask()` or `runTask()` with the appropriate `type` field.

## Usage Patterns

### Synchronous (block until complete)

Typed helpers and `runTask()` submit a task and poll until it finishes. Max wait is 5 minutes by default.

```ts
// Typed helper -- simplest path
const task = await client.llmGenerate({
  messages: [{ role: "user", content: "Summarize quantum computing in 3 sentences" }],
});
console.log(task.status); // "completed" | "failed"
console.log(task.output);

// runTask -- same behavior, explicit type
const task2 = await client.runTask(
  { type: "screenshot", input: { url: "https://example.com" } },
  { pollIntervalMs: 2000, timeoutMs: 60_000 }
);
```

### Asynchronous (fire and forget)

Use `submitTask()` to enqueue work without blocking, then check status later or receive results via webhook.

```ts
// Submit without waiting
const { task_id } = await client.submitTask({
  type: "audio_transcribe",
  input: { url: "https://example.com/recording.mp3" },
  webhook_url: "https://yourapp.com/hooks/crowdsourcerer",
});

// Poll later
const task = await client.getTask(task_id);
if (task.status === "completed") {
  console.log(task.output);
}
```

### Cancellation

```ts
await client.cancelTask(taskId);
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
  const task = await client.webResearch({ url: "https://example.com" });
} catch (err) {
  if (err instanceof AuthError) {
    // 401 -- bad or missing API key
    console.error("Check your API key");
  } else if (err instanceof RateLimitError) {
    // 429 -- back off and retry
    console.error(`Rate limited. Retry after ${err.retryAfter}s`);
  } else if (err instanceof InsufficientCreditsError) {
    // 402 -- not enough credits
    console.error(err.message); // "Insufficient credits: need 10, have 3"
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

### Typed Task Helpers

All accept typed input and optional `{ priority?, webhook_url? }`. All block until completion.

`webResearch()`, `entityLookup()`, `documentParse()`, `dataTransform()`, `llmGenerate()`, `screenshot()`, `audioTranscribe()`, `piiDetect()`, `codeExecute()`, `webIntel()`

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

## Full Documentation

[https://crowdsourcerer.rebaselabs.online/docs](https://crowdsourcerer.rebaselabs.online/docs)

## License

MIT
