# CrowdSorcerer Python SDK

Official Python SDK for [CrowdSorcerer](https://crowdsourcerer.rebaselabs.online) — a REST API for human work. Post labeling, moderation, ranking, and QA tasks; real workers complete them and submit typed JSON results.

## Installation

```bash
pip install crowdsourcerer-sdk
```

Requires Python 3.9+.

## Quick Start

```python
from crowdsourcerer import CrowdSorcerer

client = CrowdSorcerer(api_key="csk_your_key_here")

# Post a text classification task, ask 3 workers, wait for consensus
task = client.tasks.create("label_text", {
    "text": "The new iPhone is great!",
    "categories": ["positive", "negative", "neutral"],
    "question": "What is the sentiment?",
}, assignments_required=3)

# Poll until all workers submit (or timeout at 5 minutes)
result = client.tasks.wait(task.id)
print(result.output)  # e.g. {"summary": "positive (3/3 agree)", ...}
```

## Async Support

```python
import asyncio
from crowdsourcerer import AsyncCrowdSorcerer

async def main():
    async with AsyncCrowdSorcerer(api_key="csk_...") as client:
        # Fan out a batch of rating tasks concurrently
        pending_outputs = [...]  # AI-generated text to evaluate
        tasks = await asyncio.gather(*[
            client.tasks.create("rate_quality", {
                "content": out,
                "criteria": "Rate factual accuracy 1–5",
            }, assignments_required=2)
            for out in pending_outputs
        ])
        results = await asyncio.gather(*[client.tasks.wait(t.id) for t in tasks])
        for r in results:
            print(r.id, r.status, r.output)

asyncio.run(main())
```

## Task Types

`POST /v1/tasks` accepts the 8 human task types below:

| Type | Base credits | Description |
|------|--------------|-------------|
| `label_image` | 3 | Bounding boxes, segmentation, classification |
| `label_text` | 2 | Sentiment, categories, spam detection |
| `rate_quality` | 2 | Score content on a 1–5 (or custom) scale |
| `verify_fact` | 3 | Check a claim against sources |
| `moderate_content` | 2 | Approve / reject / escalate user content |
| `compare_rank` | 2 | Pick A vs B (or rank N) on any criterion |
| `answer_question` | 4 | Open-ended Q&A with optional context |
| `transcription_review` | 5 | Correct an AI-generated transcript |

The backend also has 6 AI primitives (`llm_generate`, `data_transform`,
`pii_detect`, `document_parse`, `code_execute`, `web_research`) but those
are **pipeline-only** — they run as steps inside `/v1/pipelines`, not as
standalone submissions. Calling `client.tasks.create()` with an AI type
directly returns a 422.

## API Reference

### `CrowdSorcerer(api_key, base_url?, timeout?, max_retries?)`

### `client.tasks`

- `.create(type, input, priority?, webhook_url?, assignments_required?, worker_reward_credits?, task_instructions?, claim_timeout_minutes?)` → `TaskCreateResponse`
- `.create_batch(tasks)` → `BatchTaskCreateResponse` (up to 50 at a time)
- `.get(task_id)` → `Task`
- `.list(limit?, offset?, status?, type?)` → `PaginatedTasks`
- `.wait(task_id, poll_interval?, timeout?)` → `Task` — polls until completed / failed
- `.cancel(task_id)`

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
from crowdsourcerer import (
    CrowdSorcerer, AuthError, RateLimitError,
    InsufficientCreditsError, TaskError,
)

client = CrowdSorcerer(api_key="csk_...")

try:
    task = client.tasks.create("verify_fact", {
        "claim": "The Eiffel Tower is 330 metres tall",
    })
    result = client.tasks.wait(task.id)
except InsufficientCreditsError:
    print("Top up at crowdsourcerer.rebaselabs.online/dashboard/credits")
except RateLimitError as e:
    print(f"Rate limited, retry after {e.retry_after}s")
except TaskError as e:
    print(f"Task failed: {e}")
except AuthError:
    print("Invalid API key")
```

## Consensus across multiple workers

Any human task accepts `assignments_required` (1–20) plus a
`consensus_strategy`. The task stays open until enough workers submit,
then the strategy decides how the result is computed.

```python
task = client.tasks.create(
    "rate_quality",
    {"content": "<ai output>", "criteria": "Rate 1–5 on factual accuracy"},
    assignments_required=5,
    # consensus_strategy can be "any_first" | "majority_vote"
    # | "unanimous" | "requester_review"
)
```

## Batch Uploads

```python
tasks = client.tasks.create_batch([
    {"type": "label_text", "input": {
        "text": "Great product, fast shipping!",
        "categories": ["positive", "negative", "neutral"],
        "question": "Sentiment?",
    }},
    {"type": "moderate_content", "input": {
        "content": "...",
        "content_type": "text",
        "policy_context": "Flag spam and off-topic",
    }},
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
