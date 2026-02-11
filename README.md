<!-- DOCTOC SKIP -->

<h1 align="center">
 <a href="https://www.testcharmvision.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/testcharmvision_logo.png"/>
    <img height="120" src="fern/images/testcharmvision_logo_blackbg.png"/>
  </picture>
 </a>
 <br />
</h1>
<p align="center">
🐉 Automate Browser-based workflows using LLMs and Computer Vision 🐉
</p>
<p align="center">
  <a href="https://www.testcharmvision.com/"><img src="https://img.shields.io/badge/Website-blue?logo=googlechrome&logoColor=black"/></a>
  <a href="https://www.testcharmvision.com/docs/"><img src="https://img.shields.io/badge/Docs-yellow?logo=gitbook&logoColor=black"/></a>
  <a href="https://discord.gg/fG2XXEuQX3"><img src="https://img.shields.io/discord/1212486326352617534?logo=discord&label=discord"/></a>
  <!-- <a href="https://pepy.tech/project/testcharmvision" target="_blank"><img src="https://static.pepy.tech/badge/testcharmvision" alt="Total Downloads"/></a> -->
  <a href="https://github.com/testcharmvision-ai/testcharmvision"><img src="https://img.shields.io/github/stars/testcharmvision-ai/testcharmvision" /></a>
  <a href="https://github.com/Testcharmvision-AI/testcharmvision/blob/main/LICENSE"><img src="https://img.shields.io/github/license/testcharmvision-ai/testcharmvision"/></a>
  <a href="https://twitter.com/testcharmvisionai"><img src="https://img.shields.io/twitter/follow/testcharmvisionai?style=social"/></a>
  <a href="https://www.linkedin.com/company/95726232"><img src="https://img.shields.io/badge/Follow%20 on%20LinkedIn-8A2BE2?logo=linkedin"/></a>
</p>

[Testcharmvision](https://www.testcharmvision.com) automates browser-based workflows using LLMs and computer vision. It provides a Playwright-compatible SDK that adds AI functionality on top of playwright, as well as a no-code workflow builder to help both technical and non-technical users automate manual workflows on any website, replacing brittle or unreliable automation solutions.

<p align="center">
  <img src="fern/images/geico_shu_recording_cropped.gif"/>
</p>

Traditional approaches to browser automations required writing custom scripts for websites, often relying on DOM parsing and XPath-based interactions which would break whenever the website layouts changed.

Instead of only relying on code-defined XPath interactions, Testcharmvision relies on Vision LLMs to learn and interact with the websites.

# How it works
Testcharmvision was inspired by the Task-Driven autonomous agent design popularized by [BabyAGI](https://github.com/yoheinakajima/babyagi) and [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT) -- with one major bonus: we give Testcharmvision the ability to interact with websites using browser automation libraries like [Playwright](https://playwright.dev/).

Testcharmvision uses a swarm of agents to comprehend a website, and plan and execute its actions:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="fern/images/testcharmvision_2_0_system_diagram.png" />
  <img src="fern/images/testcharmvision_2_0_system_diagram.png" />
</picture>

This approach has a few advantages:

1. Testcharmvision can operate on websites it's never seen before, as it's able to map visual elements to actions necessary to complete a workflow, without any customized code
1. Testcharmvision is resistant to website layout changes, as there are no pre-determined XPaths or other selectors our system is looking for while trying to navigate
1. Testcharmvision is able to take a single workflow and apply it to a large number of websites, as it's able to reason through the interactions necessary to complete the workflow
A detailed technical report can be found [here](https://www.testcharmvision.com/blog/testcharmvision-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/).

# Demo
<!-- Redo demo -->
https://github.com/user-attachments/assets/5cab4668-e8e2-4982-8551-aab05ff73a7f

# Quickstart

## Testcharmvision Cloud
[Testcharmvision Cloud](https://app.testcharmvision.com) is a managed cloud version of Testcharmvision that allows you to run Testcharmvision without worrying about the infrastructure. It allows you to run multiple Testcharmvision instances in parallel and comes bundled with anti-bot detection mechanisms, proxy network, and CAPTCHA solvers.

If you'd like to try it out, navigate to [app.testcharmvision.com](https://app.testcharmvision.com) and create an account.

## Run Locally (UI + Server)

Choose your preferred setup method:

### Option A: pip install (Recommended)

Dependencies needed:
- [Python 3.11.x](https://www.python.org/downloads/), works with 3.12, not ready yet for 3.13
- [NodeJS & NPM](https://nodejs.org/en/download/)

Additionally, for Windows:
- [Rust](https://rustup.rs/)
- VS Code with C++ dev tools and Windows SDK

#### 1. Install Testcharmvision

```bash
pip install testcharmvision
```

#### 2. Run Testcharmvision

```bash
testcharmvision quickstart
```

If you already have a database you want to use, pass a custom connection string to skip the
local Docker PostgreSQL setup:

```bash
testcharmvision quickstart --database-string "postgresql+psycopg://user:password@localhost:5432/testcharmvision"
```

### Option B: Docker Compose

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
2. Clone the repository:
   ```bash
   git clone https://github.com/testcharmvision-ai/testcharmvision.git && cd testcharmvision
   ```
3. Run quickstart with Docker Compose:
   ```bash
   pip install testcharmvision && testcharmvision quickstart
   ```
   When prompted, choose "Docker Compose" for the full containerized setup.
4. Navigate to http://localhost:8080

## SDK

**Testcharmvision is a Playwright extension that adds AI-powered browser automation.** It gives you the full power of Playwright with additional AI capabilities—use natural language prompts to interact with elements, extract data, and automate complex multi-step workflows.

**Installation:**
- Python: `pip install testcharmvision` then run `testcharmvision quickstart` for local setup
- TypeScript: `npm install @testcharmvision/client`

### AI-Powered Page Commands

Testcharmvision adds four core AI commands directly on the page object:

| Command | Description |
|---------|-------------|
| `page.act(prompt)` | Perform actions using natural language (e.g., "Click the login button") |
| `page.extract(prompt, schema)` | Extract structured data from the page with optional JSON schema |
| `page.validate(prompt)` | Validate page state, returns `bool` (e.g., "Check if user is logged in") |
| `page.prompt(prompt, schema)` | Send arbitrary prompts to the LLM with optional response schema |

Additionally, `page.agent` provides higher-level workflow commands:

| Command | Description |
|---------|-------------|
| `page.agent.run_task(prompt)` | Execute complex multi-step tasks |
| `page.agent.login(credential_type, credential_id)` | Authenticate with stored credentials (Testcharmvision, Bitwarden, 1Password) |
| `page.agent.download_files(prompt)` | Navigate and download files |
| `page.agent.run_workflow(workflow_id)` | Execute pre-built workflows |

### AI-Augmented Playwright Actions

All standard Playwright actions support an optional `prompt` parameter for AI-powered element location:

| Action | Playwright | AI-Augmented |
|--------|------------|--------------|
| Click | `page.click("#btn")` | `page.click(prompt="Click login button")` |
| Fill | `page.fill("#email", "a@b.com")` | `page.fill(prompt="Email field", value="a@b.com")` |
| Select | `page.select_option("#country", "US")` | `page.select_option(prompt="Country dropdown", value="US")` |
| Upload | `page.upload_file("#file", "doc.pdf")` | `page.upload_file(prompt="Upload area", files="doc.pdf")` |

**Three interaction modes:**
```python
# 1. Traditional Playwright - CSS/XPath selectors
await page.click("#submit-button")

# 2. AI-powered - natural language
await page.click(prompt="Click the green Submit button")

# 3. AI fallback - tries selector first, falls back to AI if it fails
await page.click("#submit-btn", prompt="Click the Submit button")
```

### Core AI Commands - Examples

```python
# act - Perform actions using natural language
await page.act("Click the login button and wait for the dashboard to load")

# extract - Extract structured data with optional JSON schema
result = await page.extract("Get the product name and price")
result = await page.extract(
    prompt="Extract order details",
    schema={"order_id": "string", "total": "number", "items": "array"}
)

# validate - Check page state (returns bool)
is_logged_in = await page.validate("Check if the user is logged in")

# prompt - Send arbitrary prompts to the LLM
summary = await page.prompt("Summarize what's on this page")
```

### Quick Start Examples

**Run via UI:**
```bash
testcharmvision run all
```
Navigate to http://localhost:8080 to run tasks through the web interface.

**Python SDK:**
```python
from testcharmvision import Testcharmvision

# Local mode
testcharmvision = Testcharmvision.local()

# Or connect to Testcharmvision Cloud
testcharmvision = Testcharmvision(api_key="your-api-key")

# Launch browser and get page
browser = await testcharmvision.launch_cloud_browser()
page = await browser.get_working_page()

# Mix Playwright with AI-powered actions
await page.goto("https://example.com")
await page.click("#login-button")  # Traditional Playwright
await page.agent.login(credential_type="testcharmvision", credential_id="cred_123")  # AI login
await page.click(prompt="Add first item to cart")  # AI-augmented click
await page.agent.run_task("Complete checkout with: John Snow, 12345")  # AI task
```

**TypeScript SDK:**
```typescript
import { Testcharmvision } from "@testcharmvision/client";

const testcharmvision = new Testcharmvision({ apiKey: "your-api-key" });
const browser = await testcharmvision.launchCloudBrowser();
const page = await browser.getWorkingPage();

// Mix Playwright with AI-powered actions
await page.goto("https://example.com");
await page.click("#login-button");  // Traditional Playwright
await page.agent.login("testcharmvision", { credentialId: "cred_123" });  // AI login
await page.click({ prompt: "Add first item to cart" });  // AI-augmented click
await page.agent.runTask("Complete checkout with: John Snow, 12345");  // AI task

await browser.close();
```

**Simple task execution:**
```python
from testcharmvision import Testcharmvision

testcharmvision = Testcharmvision()
task = await testcharmvision.run_task(prompt="Find the top post on hackernews today")
print(task)
```

## Advanced Usage

### Control your own browser (Chrome)
> [!WARNING]
> Since [Chrome 136](https://developer.chrome.com/blog/remote-debugging-port), Chrome refuses any CDP connect to the browser using the default user_data_dir. In order to use your browser data, Testcharmvision copies your default user_data_dir to `./tmp/user_data_dir` the first time connecting to your local browser.

1. Just With Python Code
```python
from testcharmvision import Testcharmvision

# The path to your Chrome browser. This example path is for Mac.
browser_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
testcharmvision = Testcharmvision(
    base_url="http://localhost:8000",
    api_key="YOUR_API_KEY",
    browser_path=browser_path,
)
task = await testcharmvision.run_task(
    prompt="Find the top post on hackernews today",
)
```

2. With Testcharmvision Service

Add two variables to your .env file:
```bash
# The path to your Chrome browser. This example path is for Mac.
CHROME_EXECUTABLE_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BROWSER_TYPE=cdp-connect
```

Restart Testcharmvision service `testcharmvision run all` and run the task through UI or code

### Run Testcharmvision with any remote browser
Grab the cdp connection url and pass it to Testcharmvision

```python
from testcharmvision import Testcharmvision

testcharmvision = Testcharmvision(cdp_url="your cdp connection url")
task = await testcharmvision.run_task(
    prompt="Find the top post on hackernews today",
)
```

### Get consistent output schema from your run
You can do this by adding the `data_extraction_schema` parameter:
```python
from testcharmvision import Testcharmvision

testcharmvision = Testcharmvision()
task = await testcharmvision.run_task(
    prompt="Find the top post on hackernews today",
    data_extraction_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "The title of the top post"
            },
            "url": {
                "type": "string",
                "description": "The URL of the top post"
            },
            "points": {
                "type": "integer",
                "description": "Number of points the post has received"
            }
        }
    }
)
```

### Helpful commands to debug issues


```bash
# Launch the Testcharmvision Server Separately*
testcharmvision run server

# Launch the Testcharmvision UI
testcharmvision run ui

# Check status of the Testcharmvision service
testcharmvision status

# Stop the Testcharmvision service
testcharmvision stop all

# Stop the Testcharmvision UI
testcharmvision stop ui

# Stop the Testcharmvision Server Separately
testcharmvision stop server
```

# Performance & Evaluation

Testcharmvision has SOTA performance on the [WebBench benchmark](webbench.ai) with a 64.4% accuracy. The technical report + evaluation can be found [here](https://www.testcharmvision.com/blog/web-bench-a-new-way-to-compare-ai-browser-agents/)

<p align="center">
  <img src="fern/images/performance/webbench_overall.png"/>
</p>

## Performance on WRITE tasks (eg filling out forms, logging in, downloading files, etc)

Testcharmvision is the best performing agent on WRITE tasks (eg filling out forms, logging in, downloading files, etc), which is primarily used for RPA (Robotic Process Automation) adjacent tasks.

<p align="center">
  <img src="fern/images/performance/webbench_write.png"/>
</p>

# Testcharmvision Features

## Testcharmvision Tasks
Tasks are the fundamental building block inside Testcharmvision. Each task is a single request to Testcharmvision, instructing it to navigate through a website and accomplish a specific goal.

Tasks require you to specify a `url`, `prompt`, and can optionally include a `data schema` (if you want the output to conform to a specific schema) and `error codes` (if you want Testcharmvision to stop running in specific situations).

<p align="center">
  <img src="fern/images/testcharmvision_2_0_screenshot.png"/>
</p>


## Testcharmvision Workflows
Workflows are a way to chain multiple tasks together to form a cohesive unit of work.

For example, if you wanted to download all invoices newer than January 1st, you could create a workflow that first navigated to the invoices page, then filtered down to only show invoices newer than January 1st, extracted a list of all eligible invoices, and iterated through each invoice to download it.

Another example is if you wanted to automate purchasing products from an e-commerce store, you could create a workflow that first navigated to the desired product, then added it to a cart. Second, it would navigate to the cart and validate the cart state. Finally, it would go through the checkout process to purchase the items.

Supported workflow features include:
1. Browser Task
1. Browser Action
1. Data Extraction
1. Validation
1. For Loops
1. File parsing
1. Sending emails
1. Text Prompts
1. HTTP Request Block
1. Custom Code Block
1. Uploading files to block storage
1. (Coming soon) Conditionals

<p align="center">
  <img src="fern/images/block_example_v2.png"/>
</p>

## Livestreaming
Testcharmvision allows you to livestream the viewport of the browser to your local machine so that you can see exactly what Testcharmvision is doing on the web. This is useful for debugging and understanding how Testcharmvision is interacting with a website, and intervening when necessary

## Form Filling
Testcharmvision is natively capable of filling out form inputs on websites. Passing in information via the `navigation_goal` will allow Testcharmvision to comprehend the information and fill out the form accordingly.

## Data Extraction
Testcharmvision is also capable of extracting data from a website.

You can also specify a `data_extraction_schema` directly within the main prompt to tell Testcharmvision exactly what data you'd like to extract from the website, in jsonc format. Testcharmvision's output will be structured in accordance to the supplied schema.

## File Downloading
Testcharmvision is also capable of downloading files from a website. All downloaded files are automatically uploaded to block storage (if configured), and you can access them via the UI.

## Authentication
Testcharmvision supports a number of different authentication methods to make it easier to automate tasks behind a login. If you'd like to try it out, please reach out to us [via email](mailto:founders@testcharmvision.com) or [discord](https://discord.gg/fG2XXEuQX3).

<p align="center">
  <img src="fern/images/secure_password_task_example.png"/>
</p>


### 🔐 2FA Support (TOTP)
Testcharmvision supports a number of different 2FA methods to allow you to automate workflows that require 2FA.

Examples include:
1. QR-based 2FA (e.g. Google Authenticator, Authy)
1. Email based 2FA
1. SMS based 2FA

🔐 Learn more about 2FA support [here](https://www.testcharmvision.com/docs/credentials/totp).

### Password Manager Integrations
Testcharmvision currently supports the following password manager integrations:
- [x] Bitwarden
- [x] Custom Credential Service (HTTP API)
- [ ] 1Password
- [ ] LastPass


## Model Context Protocol (MCP)
Testcharmvision supports the Model Context Protocol (MCP) to allow you to use any LLM that supports MCP.

See the MCP documentation [here](https://github.com/Testcharmvision-AI/testcharmvision/blob/main/integrations/mcp/README.md)

## Zapier / Make.com / N8N Integration
Testcharmvision supports Zapier, Make.com, and N8N to allow you to connect your Testcharmvision workflows to other apps.

* [Zapier](https://www.testcharmvision.com/docs/integrations/zapier)
* [Make.com](https://www.testcharmvision.com/docs/integrations/make.com)
* [N8N](https://www.testcharmvision.com/docs/integrations/n8n)

🔐 Learn more about 2FA support [here](https://www.testcharmvision.com/docs/credentials/totp).


# Real-world examples of Testcharmvision
We love to see how Testcharmvision is being used in the wild. Here are some examples of how Testcharmvision is being used to automate workflows in the real world. Please open PRs to add your own examples!

## Invoice Downloading on many different websites
[Book a demo to see it live](https://meetings.hubspot.com/testcharmvision/demo)

<p align="center">
  <img src="fern/images/invoice_downloading.gif"/>
</p>

## Automate the job application process
[💡 See it in action](https://app.testcharmvision.com/tasks/create/job_application)
<p align="center">
  <img src="fern/images/job_application_demo.gif"/>
</p>

## Automate materials procurement for a manufacturing company
[💡 See it in action](https://app.testcharmvision.com/tasks/create/finditparts)
<p align="center">
  <img src="fern/images/finditparts_recording_crop.gif"/>
</p>

## Navigating to government websites to register accounts or fill out forms
[💡 See it in action](https://app.testcharmvision.com/tasks/create/california_edd)
<p align="center">
  <img src="fern/images/edd_services.gif"/>
</p>
<!-- Add example of delaware entity lookups x2 -->

## Filling out random contact us forms
[💡 See it in action](https://app.testcharmvision.com/tasks/create/contact_us_forms)
<p align="center">
  <img src="fern/images/contact_forms.gif"/>
</p>


## Retrieving insurance quotes from insurance providers in any language
[💡 See it in action](https://app.testcharmvision.com/tasks/create/bci_seguros)
<p align="center">
  <img src="fern/images/bci_seguros_recording.gif"/>
</p>

[💡 See it in action](https://app.testcharmvision.com/tasks/create/geico)

<p align="center">
  <img src="fern/images/geico_shu_recording_cropped.gif"/>
</p>

# Contributor Setup
Make sure to have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed.
1. Run this to create your virtual environment (`.venv`)
    ```bash
    uv sync --group dev
    ```
2. Perform initial server configuration
    ```bash
    uv run testcharmvision quickstart
    ```
3. Navigate to `http://localhost:8080` in your browser to start using the UI
   *The Testcharmvision CLI supports Windows, WSL, macOS, and Linux environments.*

# Documentation

More extensive documentation can be found on our [📕 docs page](https://www.testcharmvision.com/docs). Please let us know if something is unclear or missing by opening an issue or reaching out to us [via email](mailto:founders@testcharmvision.com) or [discord](https://discord.gg/fG2XXEuQX3).

# Supported LLMs
| Provider | Supported Models |
| -------- | ------- |
| OpenAI   | GPT-5, GPT-5.2, GPT-4.1, o3, o4-mini |
| Anthropic | Claude 4 (Sonnet, Opus), Claude 4.5 (Haiku, Sonnet, Opus) |
| Azure OpenAI | Any GPT models. Better performance with a multimodal llm (azure/gpt4-o) |
| AWS Bedrock | Claude 3.5, Claude 3.7, Claude 4 (Sonnet, Opus), Claude 4.5 (Sonnet, Opus) |
| Gemini | Gemini 3 Pro/Flash, Gemini 2.5 Pro/Flash |
| Ollama | Run any locally hosted model via [Ollama](https://github.com/ollama/ollama) |
| OpenRouter | Access models through [OpenRouter](https://openrouter.ai) |
| OpenAI-compatible | Any custom API endpoint that follows OpenAI's API format (via [liteLLM](https://docs.litellm.ai/docs/providers/openai_compatible)) |

#### Environment Variables

##### OpenAI
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_OPENAI`| Register OpenAI models | Boolean | `true`, `false` |
| `OPENAI_API_KEY` | OpenAI API Key | String | `sk-1234567890` |
| `OPENAI_API_BASE` | OpenAI API Base, optional | String | `https://openai.api.base` |
| `OPENAI_ORGANIZATION` | OpenAI Organization ID, optional | String | `your-org-id` |

Recommended `LLM_KEY`: `OPENAI_GPT5`, `OPENAI_GPT5_2`, `OPENAI_GPT4_1`, `OPENAI_O3`, `OPENAI_O4_MINI`

##### Anthropic
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_ANTHROPIC` | Register Anthropic models| Boolean | `true`, `false` |
| `ANTHROPIC_API_KEY` | Anthropic API key| String | `sk-1234567890` |

Recommended `LLM_KEY`: `ANTHROPIC_CLAUDE4.5_OPUS`, `ANTHROPIC_CLAUDE4.5_SONNET`, `ANTHROPIC_CLAUDE4_OPUS`, `ANTHROPIC_CLAUDE4_SONNET`

##### Azure OpenAI
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_AZURE` | Register Azure OpenAI models | Boolean | `true`, `false` |
| `AZURE_API_KEY` | Azure deployment API key | String | `sk-1234567890` |
| `AZURE_DEPLOYMENT` | Azure OpenAI Deployment Name | String | `testcharmvision-deployment`|
| `AZURE_API_BASE` | Azure deployment api base url| String | `https://testcharmvision-deployment.openai.azure.com/`|
| `AZURE_API_VERSION` | Azure API Version| String | `2024-02-01`|

Recommended `LLM_KEY`: `AZURE_OPENAI`

##### AWS Bedrock
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_BEDROCK` | Register AWS Bedrock models. To use AWS Bedrock, you need to make sure your [AWS configurations](https://github.com/boto/boto3?tab=readme-ov-file#using-boto3) are set up correctly first. | Boolean | `true`, `false` |

Recommended `LLM_KEY`: `BEDROCK_ANTHROPIC_CLAUDE4.5_OPUS_INFERENCE_PROFILE`, `BEDROCK_ANTHROPIC_CLAUDE4.5_SONNET_INFERENCE_PROFILE`, `BEDROCK_ANTHROPIC_CLAUDE4_OPUS_INFERENCE_PROFILE`

##### Gemini
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_GEMINI` | Register Gemini models| Boolean | `true`, `false` |
| `GEMINI_API_KEY` | Gemini API Key| String | `your_google_gemini_api_key`|

Recommended `LLM_KEY`: `GEMINI_2.5_PRO`, `GEMINI_2.5_FLASH`, `GEMINI_2.5_PRO_PREVIEW`, `GEMINI_2.5_FLASH_PREVIEW`

##### Ollama
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_OLLAMA`| Register local models via Ollama | Boolean | `true`, `false` |
| `OLLAMA_SERVER_URL` | URL for your Ollama server | String | `http://host.docker.internal:11434` |
| `OLLAMA_MODEL` | Ollama model name to load | String | `qwen2.5:7b-instruct` |
| `OLLAMA_SUPPORTS_VISION` | Enable vision support | Boolean | `true`, `false` |

Recommended `LLM_KEY`: `OLLAMA`

Note: Set `OLLAMA_SUPPORTS_VISION=true` for vision models like qwen3-vl, llava, etc.

##### OpenRouter
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_OPENROUTER`| Register OpenRouter models | Boolean | `true`, `false` |
| `OPENROUTER_API_KEY` | OpenRouter API key | String | `sk-1234567890` |
| `OPENROUTER_MODEL` | OpenRouter model name | String | `mistralai/mistral-small-3.1-24b-instruct` |
| `OPENROUTER_API_BASE` | OpenRouter API base URL | String | `https://api.openrouter.ai/v1` |

Recommended `LLM_KEY`: `OPENROUTER`

##### OpenAI-Compatible
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `ENABLE_OPENAI_COMPATIBLE`| Register a custom OpenAI-compatible API endpoint | Boolean | `true`, `false` |
| `OPENAI_COMPATIBLE_MODEL_NAME` | Model name for OpenAI-compatible endpoint | String | `yi-34b`, `gpt-3.5-turbo`, `mistral-large`, etc.|
| `OPENAI_COMPATIBLE_API_KEY` | API key for OpenAI-compatible endpoint | String | `sk-1234567890`|
| `OPENAI_COMPATIBLE_API_BASE` | Base URL for OpenAI-compatible endpoint | String | `https://api.together.xyz/v1`, `http://localhost:8000/v1`, etc.|
| `OPENAI_COMPATIBLE_API_VERSION` | API version for OpenAI-compatible endpoint, optional| String | `2023-05-15`|
| `OPENAI_COMPATIBLE_MAX_TOKENS` | Maximum tokens for completion, optional| Integer | `4096`, `8192`, etc.|
| `OPENAI_COMPATIBLE_TEMPERATURE` | Temperature setting, optional| Float | `0.0`, `0.5`, `0.7`, etc.|
| `OPENAI_COMPATIBLE_SUPPORTS_VISION` | Whether model supports vision, optional| Boolean | `true`, `false`|

Supported LLM Key: `OPENAI_COMPATIBLE`

##### General LLM Configuration
| Variable | Description| Type | Sample Value|
| -------- | ------- | ------- | ------- |
| `LLM_KEY` | The name of the model you want to use | String | See supported LLM keys above |
| `SECONDARY_LLM_KEY` | The name of the model for mini agents testcharmvision runs with | String | See supported LLM keys above |
| `LLM_CONFIG_MAX_TOKENS` | Override the max tokens used by the LLM | Integer | `128000` |

# Feature Roadmap
This is our planned roadmap for the next few months. If you have any suggestions or would like to see a feature added, please don't hesitate to reach out to us [via email](mailto:founders@testcharmvision.com) or [discord](https://discord.gg/fG2XXEuQX3).

- [x] **Open Source** - Open Source Testcharmvision's core codebase
- [x] **Workflow support** - Allow support to chain multiple Testcharmvision calls together
- [x] **Improved context** - Improve Testcharmvision's ability to understand content around interactable elements by introducing feeding relevant label context through the text prompt
- [x] **Cost Savings** - Improve Testcharmvision's stability and reduce the cost of running Testcharmvision by optimizing the context tree passed into Testcharmvision
- [x] **Self-serve UI** - Deprecate the Streamlit UI in favour of a React-based UI component that allows users to kick off new jobs in Testcharmvision
- [x] **Workflow UI Builder** - Introduce a UI to allow users to build and analyze workflows visually
- [x] **Chrome Viewport streaming** - Introduce a way to live-stream the Chrome viewport to the user's browser (as a part of the self-serve UI)
- [x] **Past Runs UI** - Deprecate the Streamlit UI in favour of a React-based UI that allows you to visualize past runs and their results
- [X] **Auto workflow builder ("Observer") mode** - Allow Testcharmvision to auto-generate workflows as it's navigating the web to make it easier to build new workflows
- [x] **Prompt Caching** - Introduce a caching layer to the LLM calls to dramatically reduce the cost of running Testcharmvision (memorize past actions and repeat them!)
- [x] **Web Evaluation Dataset** - Integrate Testcharmvision with public benchmark tests to track the quality of our models over time
- [ ] **Improved Debug mode** - Allow Testcharmvision to plan its actions and get "approval" before running them, allowing you to debug what it's doing and more easily iterate on the prompt
- [ ] **Chrome Extension** - Allow users to interact with Testcharmvision through a Chrome extension (incl voice mode, saving tasks, etc.)
- [ ] **Testcharmvision Action Recorder** - Allow Testcharmvision to watch a user complete a task and then automatically generate a workflow for it
- [ ] **Interactable Livestream** - Allow users to interact with the livestream in real-time to intervene when necessary (such as manually submitting sensitive forms)
- [ ] **Integrate LLM Observability tools** - Integrate LLM Observability tools to allow back-testing prompt changes with specific data sets + visualize the performance of Testcharmvision over time
- [x] **Langchain Integration** - Create langchain integration in langchain_community to use Testcharmvision as a "tool".

# Contributing

We welcome PRs and suggestions! Don't hesitate to open a PR/issue or to reach out to us [via email](mailto:founders@testcharmvision.com) or [discord](https://discord.gg/fG2XXEuQX3).
Please have a look at our [contribution guide](CONTRIBUTING.md) and
["Help Wanted" issues](https://github.com/testcharmvision-ai/testcharmvision/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22) to get started!

If you want to chat with the testcharmvision repository to get a high level overview of how it is structured, how to build off it, and how to resolve usage questions, check out [Code Sage](https://sage.storia.ai?utm_source=github&utm_medium=referral&utm_campaign=testcharmvision-readme).

# Telemetry

By Default, Testcharmvision collects basic usage statistics to help us understand how Testcharmvision is being used. If you would like to opt-out of telemetry, please set the `TESTCHARMVISION_TELEMETRY` environment variable to `false`.

# License
Testcharmvision's open source repository is supported via a managed cloud. All of the core logic powering Testcharmvision is available in this open source repository licensed under the [AGPL-3.0 License](LICENSE), with the exception of anti-bot measures available in our managed cloud offering.

If you have any questions or concerns around licensing, please [contact us](mailto:support@testcharmvision.com) and we would be happy to help.

# Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Testcharmvision-AI/testcharmvision&type=Date)](https://star-history.com/#Testcharmvision-AI/testcharmvision&Date)
