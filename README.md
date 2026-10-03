<!-- DOCTOC SKIP -->
<!-- prettier-ignore -->
<div align="center">

<a href="https://www.skyvern.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_logo.png"/>
    <img height="96" src="fern/images/skyvern_logo_blackbg.png" alt="Skyvern logo"/>
  </picture>
</a>

# Skyvern

*Automate browser-based workflows using LLMs and computer vision*

[![PyPI version](https://img.shields.io/pypi/v/skyvern?style=flat-square&label=PyPI)](https://pypi.org/project/skyvern/)
[![npm version](https://img.shields.io/npm/v/@skyvern/client?style=flat-square&label=npm)](https://www.npmjs.com/package/@skyvern/client)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/Docs-skyvern.com-07C983?style=flat-square)](https://www.skyvern.com/docs)
[![Discord](https://img.shields.io/discord/1212486326352617534?style=flat-square&logo=discord&label=Discord&color=5865f2&logoColor=fff)](https://discord.gg/fG2XXEuQX3)
[![License](https://img.shields.io/github/license/skyvern-ai/skyvern?style=flat-square)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/skyvern-ai/skyvern?style=flat-square)](https://github.com/skyvern-ai/skyvern/stargazers)
[![Follow on X](https://img.shields.io/twitter/follow/skyvernai?style=flat-square)](https://twitter.com/skyvernai)

⭐ If you like Skyvern, star it on GitHub — it helps a lot!

[Overview](#overview) • [Features](#features) • [Getting started](#getting-started) • [Usage](#usage) • [Examples](#examples) • [Resources](#resources)

<img src="fern/images/geico_shu_recording_cropped.gif" alt="Skyvern filling out an insurance quote form on Geico.com" width="720"/>

</div>

[Skyvern](https://www.skyvern.com) lets you automate any website with natural language. Describe what you want done, and Skyvern opens a real browser, reads the page visually, plans the next step with an LLM, and executes it with [Playwright](https://playwright.dev/). No selectors, no per-site scripts, no breakage when the layout changes.

You can use it as a **Playwright-compatible SDK** with AI superpowers (Python and TypeScript), a **REST API**, a **CLI**, an **MCP server** for AI assistants, or through the **no-code workflow builder** in the web UI.

> [!TIP]
> Want to try it without installing anything? Sign up at [app.skyvern.com](https://app.skyvern.com) and run your first task from the browser. Skyvern Cloud comes with managed browsers, residential proxies, anti-bot detection and CAPTCHA solving.

## Overview

Traditional browser automation means writing custom scripts for every website, usually built on DOM parsing and XPath selectors that break as soon as the page changes.

Skyvern takes a different approach. Instead of relying on pre-defined selectors, it uses vision-capable LLMs to understand the page and figure out the interactions needed to complete your goal, the same way a person would.

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern-system-diagram-dark.png"/>
    <img src="fern/images/skyvern-system-diagram-light.png" alt="Skyvern system diagram" width="720"/>
  </picture>
</div>

For each step, Skyvern runs a perception-action loop:

1. **Screenshot**: capture the current state of the page.
2. **Analyze**: send the screenshot and the DOM to the LLM to identify interactive elements and decide the next action.
3. **Execute**: perform the action in the browser (click, type, select, scroll, extract, download).
4. **Repeat** until the goal is met, a validation fails, or the step limit is reached.

This is why Skyvern can:

- **Work on websites it has never seen before**, mapping what it sees to the actions required, without any site-specific code.
- **Survive layout changes**, since there are no hard-coded XPaths or CSS selectors to go stale.
- **Apply a single workflow to many websites**, reasoning through each site's differences on the fly.
- **Handle ambiguity**, for example inferring "Were you eligible to drive at 18?" from a license issued at 16.

Skyvern was inspired by the task-driven agent designs popularized by [BabyAGI](https://github.com/yoheinakajima/babyagi) and [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT), with one major addition: the ability to actually interact with websites through a browser. Read the [Skyvern 2.0 technical report](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/) for the details.

### Performance

Skyvern has state-of-the-art results on the [WebBench](https://webbench.ai) benchmark with **64.4% overall accuracy**, and is the best-performing agent on **WRITE** tasks (form filling, logging in, downloading files), the category that matters most for RPA-style automation. See the [full evaluation](https://www.skyvern.com/blog/web-bench-a-new-way-to-compare-ai-browser-agents/).

<div align="center">
  <img src="fern/images/performance/webbench_overall.png" alt="WebBench overall accuracy" width="600"/>
</div>

### Demo

https://github.com/user-attachments/assets/5cab4668-e8e2-4982-8551-aab05ff73a7f

## Features

- **Natural-language tasks**: give Skyvern a URL and a prompt, get back a completed task with structured output.
- **Playwright with AI built in**: every Playwright action accepts an optional `prompt`, and the page gains `act`, `extract`, `validate` and `prompt` commands. Use selectors, natural language, or selectors with AI fallback.
- **Workflows**: chain tasks, loops, conditionals, code, HTTP requests, file handling and email into repeatable multi-step automations, from the UI or the API.
- **Structured data extraction**: pass a JSON schema and get typed, consistent output every run.
- **Forms, files and downloads**: fill out complex multi-page forms, upload documents, and download files straight to block storage.
- **Authentication and 2FA**: log in with credentials from Skyvern's vault, Bitwarden, 1Password or Azure Key Vault, with TOTP, email and SMS based 2FA support.
- **Persistent browser sessions and profiles**: keep cookies and login state across runs.
- **Bring your own browser**: drive a Chrome instance on your machine over CDP, locally or from Skyvern Cloud through a tunnel.
- **Livestreaming and recordings**: watch the browser in real time, then replay every step with screenshots and the LLM's reasoning.
- **Integrations**: MCP server for Claude, Cursor, Windsurf and others, plus Zapier, Make, n8n and Workato.
- **Any LLM**: OpenAI, Anthropic, Gemini, Azure OpenAI, AWS Bedrock, xAI, Ollama, OpenRouter and any OpenAI-compatible endpoint.

## Getting started

There are three ways to use Skyvern. Pick the one that fits:

| | Best for | Install |
|---|---|---|
| **[Skyvern Cloud](#skyvern-cloud)** | Getting started fast, production scale, no infrastructure | None |
| **[Python or TypeScript SDK](#python-and-typescript-sdk)** | Driving Skyvern Cloud (or any Skyvern server) from code | `pip install skyvern` or `npm install @skyvern/client` |
| **[Self-hosted](#self-hosted)** | Full data control, your own LLM keys, air-gapped networks | Docker Compose or `pip install "skyvern[server]"` |

### Skyvern Cloud

1. Sign up at [app.skyvern.com](https://app.skyvern.com).
2. Type a task such as `Navigate to the Hacker News homepage and get the top 3 posts` and press run.
3. Grab your API key from [Settings](https://app.skyvern.com/settings) when you're ready to use the SDK.

### Python and TypeScript SDK

Requires Python 3.11+ or Node.js 18+.

```bash
pip install skyvern            # Python
npm install @skyvern/client    # TypeScript
```

Run your first task against Skyvern Cloud:

```python
import asyncio
from skyvern import Skyvern

async def main():
    skyvern = Skyvern(api_key="YOUR_API_KEY")
    result = await skyvern.run_task(
        prompt="Get the title of the top post on Hacker News",
        url="https://news.ycombinator.com",
        wait_for_completion=True,
    )
    print(result.output)

asyncio.run(main())
```

> [!NOTE]
> The base `skyvern` package is a lightweight client for Skyvern Cloud and remote servers. Two extras unlock local execution:
>
> | Extra | What it adds |
> |---|---|
> | `pip install "skyvern[local]"` | Embedded mode: `Skyvern.local()` and `launch_local_browser()` run the agent in-process with a local Chromium. Run `python -m playwright install chromium` afterwards. |
> | `pip install "skyvern[server]"` | Self-hosted API server, `skyvern quickstart`, and the local MCP server. |

### Self-hosted

Self-hosting runs everything on your machine: the API server with an embedded Chromium browser, the web UI, and a database. You bring the LLM API key.

#### Option A: Docker Compose (recommended)

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) and at least 4 GB of RAM.

1. Clone the repository:

   ```bash
   git clone https://github.com/Skyvern-AI/skyvern.git && cd skyvern
   ```

2. Create the environment files and add your LLM provider to `.env`:

   ```bash
   cp .env.example .env
   cp skyvern-frontend/.env.example skyvern-frontend/.env
   ```

   ```bash
   # .env
   ENABLE_OPENAI=true
   OPENAI_API_KEY=sk-...
   LLM_KEY=OPENAI_GPT5_5
   ```

3. Start the stack:

   ```bash
   docker compose up -d
   ```

4. Open http://localhost:8080. The API is available at http://localhost:8000, and your local API key is written to `.skyvern/credentials.toml` on first start.

See the [Docker setup guide](https://www.skyvern.com/docs/developers/self-hosted/docker) for the full walkthrough and the [Kubernetes guide](https://www.skyvern.com/docs/developers/self-hosted/kubernetes) for production deployments.

#### Option B: pip install

Requires Python 3.11+. On Windows you also need [Rust](https://rustup.rs/) and the Visual Studio C++ build tools.

```bash
pip install "skyvern[server]"
skyvern quickstart
```

The setup wizard walks you through choosing an LLM provider, configuring the browser and connecting your AI tools. It defaults to a SQLite database at `~/.skyvern/data.db`, so no Postgres or Docker is required. Pass `--database-string=postgresql+psycopg://user:pass@host:5432/dbname` to use an existing Postgres instead.

Alternatively, the one-line installer sets up an isolated environment with [uv](https://docs.astral.sh/uv/) and runs the wizard for you:

```bash
curl -LsSf https://install.skyvern.com | sh
```

> [!IMPORTANT]
> The pip install runs the backend only. For the web UI use Docker Compose, or run from a source checkout as described below.

#### Option C: Run from source

Use this if you want to hack on Skyvern itself. Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js.

```bash
git clone https://github.com/Skyvern-AI/skyvern.git && cd skyvern
uv sync --group dev
uv run skyvern quickstart
```

Then open http://localhost:8080. Useful commands once you're set up:

```bash
skyvern run all       # start the API server and the UI
skyvern run server    # API server only
skyvern run ui        # UI only
skyvern status        # what's running?
skyvern stop all      # stop everything
```

### Configure your LLM

Self-hosted Skyvern needs at least one LLM provider. Enable it in `.env` and pick a model with `LLM_KEY`. `SECONDARY_LLM_KEY` optionally routes lightweight calls to a cheaper model.

| Provider | Example `LLM_KEY` |
|---|---|
| OpenAI | `OPENAI_GPT5_5`, `OPENAI_GPT4_1`, `OPENAI_O3` |
| Anthropic | `ANTHROPIC_CLAUDE4.7_OPUS`, `ANTHROPIC_CLAUDE4.6_SONNET` |
| Google Gemini | `GEMINI_3_PRO`, `GEMINI_3.0_FLASH` |
| Azure OpenAI | Any GPT deployment in your subscription |
| Amazon Bedrock | `BEDROCK_ANTHROPIC_CLAUDE4.7_OPUS_INFERENCE_PROFILE` |
| xAI | `XAI_GROK_4_5` |
| Ollama | Any locally hosted vision model |
| OpenRouter, Groq, OpenAI-compatible | Any endpoint that speaks the OpenAI API, via [LiteLLM](https://docs.litellm.ai/docs/providers/openai_compatible) |

The complete list of keys, environment variables and multi-model setups is in the [LLM configuration docs](https://www.skyvern.com/docs/developers/self-hosted/llm-configuration).

> [!NOTE]
> Skyvern collects basic anonymous usage statistics to help us understand how it's used. Set `SKYVERN_TELEMETRY=false` to opt out.

## Usage

### Run a task

A task is the basic unit of work: a starting URL, a prompt, and optionally a schema for the output. The same call works against Skyvern Cloud or a self-hosted server.

<details open>
<summary><b>Python</b></summary>

```python
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")
# Self-hosted: Skyvern(base_url="http://localhost:8000", api_key="YOUR_LOCAL_API_KEY")

task = await skyvern.run_task(
    prompt="Find the top post on Hacker News today",
    url="https://news.ycombinator.com",
    data_extraction_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "points": {"type": "integer"},
        },
    },
    wait_for_completion=True,
)
print(task.output)
```

</details>

<details>
<summary><b>TypeScript</b></summary>

```typescript
import { Skyvern } from "@skyvern/client";

const skyvern = new Skyvern({ apiKey: "YOUR_API_KEY" });

const task = await skyvern.runTask({
  body: {
    prompt: "Find the top post on Hacker News today",
    url: "https://news.ycombinator.com",
  },
  waitForCompletion: true,
});
console.log(task.output);
```

</details>

<details>
<summary><b>cURL</b></summary>

```bash
curl -X POST "https://api.skyvern.com/v1/run/tasks" \
  -H "x-api-key: $SKYVERN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Find the top post on Hacker News today",
    "url": "https://news.ycombinator.com"
  }'
```

The request returns a `run_id`. Poll `GET /v1/runs/{run_id}` for the result, or pass a `webhook_url` to be notified.

</details>

### Playwright with AI

Skyvern's browser and page objects are Playwright objects with extra AI methods. Launch a cloud browser (or a local one with `launch_local_browser()`), then mix regular Playwright calls with natural language.

```python
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")

browser = await skyvern.launch_cloud_browser()
page = await browser.get_working_page()

await page.goto("https://example.com")
await page.click("#login-button")                          # plain Playwright
await page.click(prompt="Add the first item to the cart")  # AI-powered
await page.click("#checkout", prompt="Go to checkout")     # selector first, AI fallback

await page.agent.login(credential_type="skyvern", credential_id="cred_123")
await page.agent.run_task("Complete checkout with: John Snow, 12345")

await browser.close()
```

Four AI commands live directly on the page:

| Command | Description |
|---|---|
| `page.act(prompt)` | Perform an action described in natural language |
| `page.extract(prompt, schema)` | Extract structured data, optionally matching a JSON schema |
| `page.validate(prompt)` | Check a condition on the page, returns `bool` |
| `page.prompt(prompt, schema)` | Ask the LLM anything about the current page |

Higher-level agent commands are on `page.agent`:

| Command | Description |
|---|---|
| `page.agent.run_task(prompt)` | Run a full multi-step task on this page |
| `page.agent.login(...)` | Log in with stored credentials (Skyvern vault, Bitwarden, 1Password, Azure Key Vault) |
| `page.agent.download_files(prompt)` | Navigate to and download files |
| `page.agent.run_workflow(workflow_id)` | Run a saved workflow |

The TypeScript SDK exposes the same API with `launchCloudBrowser()`, `getWorkingPage()`, `page.click({ prompt })`, `page.agent.runTask()` and friends. See the [SDK reference](https://www.skyvern.com/docs/sdk-reference/complete-reference) for every method.

### Workflows

Workflows chain blocks into a repeatable automation, for example: go to the invoices page, filter to this month, loop over every invoice, download it, and email a summary. Build them visually in the UI or define them as JSON through the API and CLI.

<div align="center">
  <img src="fern/images/block_example_v2.png" alt="Workflow blocks in the Skyvern UI" width="720"/>
</div>

Available blocks include:

- **Browser**: navigation, action, extraction, validation, login, task, go to URL, file download, file upload, wait
- **Control flow**: for loop, while loop, conditional, human interaction, workflow trigger
- **Data and files**: text prompt, code, HTTP request, web search, file parser, PDF parser, PDF fill, PDF split, data export, Google Sheets read and write
- **Communication**: send email, email inbox

Details and parameters for every block are in the [blocks reference](https://www.skyvern.com/docs/developers/browser-automations/actions-reference).

### Use your own browser

Let Skyvern drive the Chrome you already use, with all your cookies, logins and extensions.

**Local Skyvern.** Enable remote debugging in Chrome at `chrome://inspect/#remote-debugging` (or let `skyvern init browser` do it for you), then either point the SDK at it:

```python
skyvern = Skyvern(base_url="http://localhost:8000", api_key="YOUR_API_KEY")
task = await skyvern.run_task(
    prompt="Download the latest invoice from my account",
    browser_address="http://127.0.0.1:9222",
)
```

or set it once in `.env` for the whole service:

```bash
BROWSER_TYPE=cdp-connect
BROWSER_REMOTE_DEBUGGING_URL=http://127.0.0.1:9222
```

**Skyvern Cloud.** Expose a local Chrome to the cloud through a tunnel, then pass the tunnel URL as `browser_address` in your tasks:

```bash
skyvern browser serve --tunnel --api-key YOUR_API_KEY
```

> [!WARNING]
> Always pass `--api-key` when using `--tunnel`. Without it, anyone with the URL has full control of your browser. See the [browser tunneling docs](https://www.skyvern.com/docs/developers/optimization/browser-tunneling).

### CLI

The `skyvern` CLI covers everything from onboarding to scripted browser control:

```bash
skyvern login                                          # authenticate with Skyvern Cloud
skyvern setup                                          # configure MCP for Claude Code, Cursor, Windsurf, ...
skyvern browser session create                         # open a cloud browser
skyvern browser navigate --url https://example.com
skyvern browser act --prompt "Click the Sign In button"
skyvern browser extract --prompt "Get all product names and prices"
skyvern workflow run --workflow-id wpid_xxx --params '{"url": "https://example.com"}'
skyvern credentials add                                # store a password or credit card securely
```

Every command supports `--json` for scripting. See the [CLI reference](https://www.skyvern.com/docs/going-to-production/cli).

### MCP server

Skyvern ships an [MCP](https://modelcontextprotocol.io/) server so AI assistants such as Claude Code, Claude Desktop, Cursor and Windsurf can browse the web, fill forms and run workflows on your behalf.

```bash
# Claude Code, hosted on Skyvern Cloud, nothing to install
claude mcp add-json skyvern '{"type":"http","url":"https://api.skyvern.com/mcp/","headers":{"x-api-key":"YOUR_SKYVERN_API_KEY"}}' --scope user

# Or let the CLI configure every tool it detects, cloud or self-hosted
pip install skyvern && skyvern login && skyvern setup
```

Setup instructions for each client are in the [MCP docs](https://www.skyvern.com/docs/developers/getting-started/mcp).

## Examples

Some of the ways Skyvern is used in the wild. Open a PR to add your own!

| Use case | |
|---|---|
| **Invoice downloading** across many different vendor portals ([book a demo](https://meetings.hubspot.com/skyvern/demo)) | <img src="fern/images/invoice_downloading.gif" width="360"/> |
| **Job applications** filled out end to end ([try it](https://app.skyvern.com/tasks/create/job_application)) | <img src="fern/images/job_application_demo.gif" width="360"/> |
| **Materials procurement** for a manufacturing company ([try it](https://app.skyvern.com/tasks/create/finditparts)) | <img src="fern/images/finditparts_recording_crop.gif" width="360"/> |
| **Government websites**: registering accounts and filing forms ([try it](https://app.skyvern.com/tasks/create/california_edd)) | <img src="fern/images/edd_services.gif" width="360"/> |
| **Contact forms** submitted on arbitrary websites ([try it](https://app.skyvern.com/tasks/create/contact_us_forms)) | <img src="fern/images/contact_forms.gif" width="360"/> |
| **Insurance quotes** retrieved from providers in any language ([try it](https://app.skyvern.com/tasks/create/bci_seguros)) | <img src="fern/images/bci_seguros_recording.gif" width="360"/> |

More step-by-step recipes are in the [cookbooks](https://www.skyvern.com/docs/cookbooks/overview).

## Troubleshooting

- **`pip install skyvern` fails to resolve dependencies** (`litellm` / `fastmcp` conflicts): upgrade to the latest release, or install with `uv pip install skyvern`.
- **`table organizations already exists` on startup**: a leftover SQLite file from an older version. Run `rm ~/.skyvern/data.db`, upgrade, and run `skyvern quickstart` again.
- **Tasks fail immediately on a self-hosted install**: check that the provider is enabled in `.env` (`ENABLE_OPENAI=true` and so on) and that `LLM_KEY` names a model from that provider.
- **UI can't reach the API**: the UI reads `VITE_API_BASE_URL` from `skyvern-frontend/.env`. Point it at `http://localhost:8000/api/v1`.

For anything else, see the [troubleshooting guide](https://www.skyvern.com/docs/developers/debugging/troubleshooting-guide) or ask on [Discord](https://discord.gg/fG2XXEuQX3).

## Resources

- [Documentation](https://www.skyvern.com/docs): quickstarts, guides and the API and SDK references
- [Core concepts](https://www.skyvern.com/docs/developers/getting-started/core-concepts): tasks, workflows, blocks, parameters, browser sessions and credentials
- [Authentication and 2FA](https://www.skyvern.com/docs/developers/features/authentication-and-2fa)
- [Browser sessions](https://www.skyvern.com/docs/developers/features/browser-sessions) and [browser profiles](https://www.skyvern.com/docs/developers/optimization/browser-profiles)
- [Webhooks](https://www.skyvern.com/docs/developers/going-to-production/webhooks) and [proxies and geo-targeting](https://www.skyvern.com/docs/developers/features/proxy-and-geo-targeting)
- Integrations: [Zapier](https://www.skyvern.com/docs/integrations/zapier), [Make](https://www.skyvern.com/docs/integrations/make), [n8n](https://www.skyvern.com/docs/integrations/n8n), [Workato](https://www.skyvern.com/docs/integrations/workato)
- [Blog](https://www.skyvern.com/blog) and the [Skyvern 2.0 technical report](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/)
- Community: [Discord](https://discord.gg/fG2XXEuQX3), [X](https://twitter.com/skyvernai), [LinkedIn](https://www.linkedin.com/company/95726232)

Found a bug or have an idea? [Open an issue](https://github.com/Skyvern-AI/skyvern/issues), or reach us at [founders@skyvern.com](mailto:founders@skyvern.com).

<div align="center">

[![Star History Chart](https://api.star-history.com/svg?repos=Skyvern-AI/skyvern&type=Date)](https://star-history.com/#Skyvern-AI/skyvern&Date)

</div>
