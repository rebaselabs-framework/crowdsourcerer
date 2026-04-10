# CrowdSorcerer Python SDK

Official Python SDK for the [CrowdSorcerer](https://crowdsourcerer.rebaselabs.online) AI task crowdsourcing platform.

## Installation

```bash
pip install crowdsourcerer-sdk
```

Requires Python 3.9+.

## Quick Start

```python
from crowdsourcerer import CrowdSorcerer

client = CrowdSorcerer(api_key="csk_your_key_here")

# Run a web research task
task = client.tasks.web_research(
    url="https://techcrunch.com/latest",
    instruction="Extract the top 3 headlines and their summaries",
)

# Wait for completion (polls every 2s, up to 5 minutes)
result = client.tasks.wait(task.id)
print(result.output)
```

## Async Support

```python
import asyncio
from crowdsourcerer import AsyncCrowdSorcerer

async def main():
    async with AsyncCrowdSorcerer(api_key="csk_...") as client:
        # Create multiple tasks concurrently
        tasks = await asyncio.gather(
            client.tasks.pii_detect("My email is alice@example.com"),
            client.tasks.screenshot("https://example.com"),
            client.tasks.llm_generate(
                messages=[{"role": "user", "content": "Write a haiku about AI"}]
            ),
        )
        # Wait for all tasks
        results = await asyncio.gather(*[client.tasks.wait(t.id) for t in tasks])

asyncio.run(main())
```

## Task Types

| Type | Credits | Description |
|------|---------|-------------|
| `web_research` | 10 | Scrape & extract from URLs |
| `entity_lookup` | 5 | Company/person enrichment |
| `document_parse` | 3 | Extract text/tables from PDFs |
| `data_transform` | 2 | Restructure data with AI |
| `llm_generate` | 1 | Run a prompt against any LLM |
| `screenshot` | 2 | Full-page screenshot |
| `audio_transcribe` | 8 | Transcribe audio/video files |
| `pii_detect` | 2 | Detect & mask PII |
| `code_execute` | 3 | Run sandboxed code |
| `web_intel` | 5 | Deep web intelligence |

Human tasks (completed by real workers):
`label_image`, `label_text`, `rate_quality`, `verify_fact`, `moderate_content`, `compare_rank`, `answer_question`, `transcription_review`

## API Reference

### `CrowdSorcerer(api_key, base_url?, timeout?, max_retries?)`

### `client.tasks`

- `.create(type, input, priority?, webhook_url?, ...)` → `TaskCreateResponse`
- `.create_batch(tasks)` → `BatchTaskCreateResponse`
- `.get(task_id)` → `Task`
- `.list(limit?, offset?, status?, type?)` → `PaginatedTasks`
- `.wait(task_id, poll_interval?, timeout?)` → `Task`
- `.cancel(task_id)`
- **Shortcuts**: `.web_research()`, `.entity_lookup()`, `.document_parse()`, `.data_transform()`, `.llm_generate()`, `.screenshot()`, `.audio_transcribe()`, `.pii_detect()`, `.code_execute()`, `.web_intel()`

### `client.credits`

- `.balance()` → `CreditBalance`
- `.transactions(limit?, offset?)` → dict

### `client.users`

- `.me()` → `User`

### `client.api_keys`

- `.list()` → `list[ApiKey]`
- `.create(name, scopes?)` → `ApiKeyCreateResponse`
- `.delete(key_id)`

## Error Handling

```python
from crowdsourcerer import CrowdSorcerer, AuthError, RateLimitError, InsufficientCreditsError, TaskError

client = CrowdSorcerer(api_key="cs_...")

try:
    task = client.tasks.create("web_research", {"url": "https://example.com"})
    result = client.tasks.wait(task.id)
except InsufficientCreditsError:
    print("Not enough credits — top up at crowdsourcerer.rebaselabs.online")
except RateLimitError as e:
    print(f"Rate limited, retry after {e.retry_after}s")
except TaskError as e:
    print(f"Task failed: {e}")
except AuthError:
    print("Invalid API key")
```

## Batch Uploads

```python
tasks = client.tasks.create_batch([
    {"type": "pii_detect", "input": {"text": "Call John at 555-0100"}},
    {"type": "screenshot", "input": {"url": "https://example.com"}},
    {"type": "llm_generate", "input": {"messages": [{"role": "user", "content": "Hi"}]}},
])
print(f"Created {tasks.summary['created']} tasks, {tasks.summary['failed']} failed")
```

## Webhook Verification

Verify incoming webhook signatures to ensure they're authentic:

```python
from crowdsourcerer import verify_webhook

# In your Flask / FastAPI handler:
sig = request.headers["X-Crowdsorcerer-Signature"]
body = request.get_data()  # raw bytes

if not verify_webhook(body, YOUR_ENDPOINT_SECRET, sig):
    abort(401, "Invalid signature")
```

During secret rotation (24-hour grace period with dual signatures):

```python
from crowdsourcerer import verify_webhook_with_rotation

is_valid = verify_webhook_with_rotation(
    payload=request.get_data(),
    current_secret=NEW_SECRET,
    previous_secret=OLD_SECRET,
    signature_header=request.headers["X-Crowdsorcerer-Signature"],
)
```

## License

MIT
