# Skyvern + Human Pages

When Skyvern's browser automation hits a blocker it cannot solve — CAPTCHAs, identity verification, phone verification, manual review gates — this integration hands the blocked step to a real human via [Human Pages](https://humanpages.ai) and returns control to Skyvern once the human finishes.

## Installation

```bash
pip install skyvern-humanpages
```

## Configuration

Set your Human Pages API key (get one at https://humanpages.ai):

```bash
export HUMANPAGES_API_KEY="your_api_key"
```

All settings can be overridden via environment variables with the `HUMANPAGES_` prefix:

| Variable | Default | Description |
|---|---|---|
| `HUMANPAGES_API_KEY` | (required) | Your Human Pages agent API key |
| `HUMANPAGES_BASE_URL` | `https://humanpages.ai` | API base URL |
| `HUMANPAGES_DEFAULT_PRICE_USDC` | `5.0` | Default job price in USDC |
| `HUMANPAGES_DEFAULT_DEADLINE_HOURS` | `4` | Default deadline for human to complete |
| `HUMANPAGES_POLL_INTERVAL_SECONDS` | `30` | How often to check job status |
| `HUMANPAGES_POLL_TIMEOUT_SECONDS` | `14400` | Maximum wait time (4 hours) |

## Quick start

```python
import asyncio
from skyvern_humanpages import HumanFallback
from skyvern_humanpages.schema import FallbackRequest, BlockerType

fallback = HumanFallback(api_key="your_api_key")

async def main():
    result = await fallback.handle_blocker(
        FallbackRequest(
            url="https://example.com/checkout",
            blocker_type=BlockerType.CAPTCHA,
            description="Solve the CAPTCHA on the checkout page so the form can be submitted.",
        )
    )
    print(f"Job {result.job_id} finished with status: {result.status}")
    print(f"Result: {result.result}")

asyncio.run(main())
```

## Using the low-level client

If you need finer control, use `HumanPagesClient` directly:

```python
import asyncio
from skyvern_humanpages.client import HumanPagesClient
from skyvern_humanpages.schema import JobCreateRequest

client = HumanPagesClient(api_key="your_api_key")

async def main():
    # Search for available humans
    humans = await client.search_humans(skill="web task")
    print(f"Found {len(humans)} available humans")

    # Create a job
    job = await client.create_job(
        JobCreateRequest(
            humanId=humans[0].id,
            title="Solve CAPTCHA on example.com",
            description="Navigate to https://example.com/checkout and solve the CAPTCHA.",
            priceUsdc=5.0,
            deadlineHours=2,
        )
    )
    print(f"Created job: {job.id}")

    # Poll for completion
    status = await client.get_job_status(job.id)
    print(f"Job status: {status.status}")

    # Read messages
    messages = await client.get_job_messages(job.id)
    for msg in messages:
        print(f"[{msg.sender}] {msg.content}")

asyncio.run(main())
```

## Supported blocker types

| Type | When to use |
|---|---|
| `captcha` | CAPTCHA challenges (reCAPTCHA, hCaptcha, etc.) |
| `identity_verification` | ID upload, selfie checks, KYC flows |
| `phone_verification` | SMS or call verification |
| `manual_review` | Human review gates, approval steps |
| `login_required` | Login needed with credentials the agent lacks |
| `other` | Any other blocker Skyvern cannot automate |

## How it works

1. Skyvern detects a blocker it cannot automate
2. This integration searches Human Pages for an available human with the right skills
3. A job is created with the blocker details, URL, and optional screenshot
4. The integration polls until the human completes the task (or it times out)
5. The result is returned to Skyvern so automation can resume

## Links

- [Human Pages](https://humanpages.ai) — hire real humans for tasks agents cannot do
- [Human Pages API docs](https://humanpages.ai/docs)
- [Skyvern](https://github.com/Skyvern-AI/skyvern) — browser automation with AI
