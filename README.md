<!-- DOCTOC SKIP -->

<h1 align="center">
 <a href="https://www.skyvern.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_logo.png"/>
    <img height="120" src="fern/images/skyvern_logo_blackbg.png"/>
  </picture>
 </a>
 <br />
</h1>

<p align="center">
  <a href="https://www.skyvern.com/"><img src="https://img.shields.io/badge/Website-blue?logo=googlechrome&logoColor=black"/></a>
  <a href="https://www.skyvern.com/docs/"><img src="https://img.shields.io/badge/Docs-yellow?logo=gitbook&logoColor=black"/></a>
  <a href="https://discord.gg/fG2XXEuQX3"><img src="https://img.shields.io/discord/1212486326352617534?logo=discord&label=discord"/></a>
  <!-- <a href="https://pepy.tech/project/skyvern" target="_blank"><img src="https://static.pepy.tech/badge/skyvern" alt="Total Downloads"/></a> -->
  <a href="https://github.com/skyvern-ai/skyvern"><img src="https://img.shields.io/github/stars/skyvern-ai/skyvern" /></a>
  <a href="https://github.com/Skyvern-AI/skyvern/blob/main/LICENSE"><img src="https://img.shields.io/github/license/skyvern-ai/skyvern"/></a>
  <a href="https://twitter.com/skyvernai"><img src="https://img.shields.io/twitter/follow/skyvernai?style=social"/></a>
  <a href="https://www.linkedin.com/company/95726232"><img src="https://img.shields.io/badge/Follow%20 on%20LinkedIn-8A2BE2?logo=linkedin"/></a>
</p>

---

Traditional tools that automate web tasks end up failing because they treat the web like it's static when it isn't. A small change on a website can break everything, leaving you with failures or having to constantly jump in to pay the "maintenance tax" of manually fixing things. 

[Skyvern](https://www.skyvern.com) is a browser automation tool that solves this problem by using AI that "sees" websites the way a human does. This means it can adapt when pages change and keep your automations running smoothly without you having to constantly babysit them. 

<!-- Demo Link-->
https://github.com/user-attachments/assets/5cab4668-e8e2-4982-8551-aab05ff73a7f
<p align="center"><i>Skyvern demo</i></p>

--- 

## Table of Contents 

- [Why Skyvern](#why-use-skyvern)
- [Use Cases](#what-can-i-use-skyvern-for)
- [How It Works](#how-does-it-work)
- [Quickstart](#quickstart)
- [SDK Reference](#sdk-quick-reference)
- [Supported LLMs](#supported-llms)
- [Reliability and Safety](#reliability--safety)
- [Documentation and Resources](#documentation-and-resources)
- [Contributing](#contributing)
- [Community & Support](#community-and-support)
- [License](#license)
- [Telemetry](#telemetry)

>  **IMPORTANT**  
Complete technical documentation, build guides, and integration patterns are available in our [docs](https://www.skyvern.com/docs).


## Why Use Skyvern?

Instead of relying solely on code-defined interactions (like DOM parsing and XPaths), Skyvern uses prompts along with computer vision and large language models (LLMs) to parse items in the viewport in real-time, plan interactions, and execute them. 

Simply put, Skyvern can recognize the intent and goal of a page and respond to it accordingly, similar to how a human would. This approach has earned Skyvern [85.8% on the WebVoyager benchmark](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/), the current state of the art for web navigation.

This gives Skyvern three properties that traditional tools can't match: 
 
- It can operate on websites it has never seen before, with no custom code required.
- It is resilient to layout changes, as no pre-defined selectors means no selectors to break.
- A single workflow generalizes across multiple websites, since Skyvern reasons about intent rather than structure.


## What Can I Use Skyvern For?

<p align="center">
  <img src="fern/images/edd_services.gif" width="600" />
</p>
<p align="center"><i>Skyvern navigating government forms. (<a href="https://app.skyvern.com/tasks/create/california_edd">See it in action</a>)</i></p>

<p align="center">
  <img src="fern/images/finditparts_recording_crop.gif" width="600" />
</p>
<p align="center"><i>Sourcing auto parts across supplier websites. (<a href="https://app.skyvern.com/tasks/create/finditparts">See it in action</a>)</i></p>

<p align="center">
  <img src="fern/images/geico_shu_recording_cropped.gif" width="600" />
</p>
<p align="center"><i>Navigating an insurance site to get a quote. (<a href="https://app.skyvern.com/sign-in?redirect_to=https%3A%2F%2Fapp.skyvern.com%2Ftasks%2Fcreate%2Fgeico">See it in action</a>)</i></p>

Skyvern can automate nearly any workflow that requires interacting with a website. Some of the common use cases include: 

- **Data extraction and scraping**  
Pull reports, exports, and structured data from portals and sites, even if they don't have APIs. 
- **Form filling & submission**  
Automate repetitive data entry points across multiple sites.
- **Invoice & document processing**  
Download invoices, receipts, and other financial documents automatically. 
- **Job applications & recruiting workflows**  
Submit applications and source candidates without manual effort. 
- **Insurance quoting & submissions**  
Request quotes across different carrier portals.
- **E-commerce operations**  
Monitor prices, update inventories, and manage orders across platforms.

> **Wanna see more?**  
> Check out more examples of how you can run Skyvern in production [here](https://www.skyvern.com/docs/getting-started/skyvern-in-action).  

## How Does It Work?

Skyvern runs a swarm of specialized agents that work together on every task. One parses the visual layout of the page and identifies interactive elements. Another plans the sequence of actions needed to reach the goal. A third executes those actions through a Playwright-compatible browser and monitors the results. If something unexpected happens, the swarm adapts in real time rather than failing.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_2_0_system_diagram.png" />
    <img src="fern/images/skyvern_2_0_system_diagram.png" width="600" />
  </picture>
</p>
<p align="center"><i>Skyvern uses a swarm of agents to comprehend a website, plan, and execute its actions.</i></p>

Skyvern exposes this as a Playwright-compatible SDK, so you can drop it into an existing automation stack without rewriting everything. Standard Playwright calls work exactly as before, and where you need AI, Skyvern's additional methods sit alongside them in the same script.

For teams that don't want to write code at all, Skyvern also provides a no-code workflow builder for constructing and running automations through a visual interface.

> **Full SDK and API Documentation**  
> For a complete list of AI-augmented Playwright actions (like fill, select_option, and upload_file) and detailed schema definitions, see our [docs](https://www.docs.skyvern.com). 

## Quickstart
Choose the path that fits how you work. Whether you want a managed environment or full local control, you can be up and running in minutes. 

### Skyvern Cloud (Fastest)

This is the easiest way to get started without having to manage infrastructure. Skyvern Cloud includes built-in anti-bot detection, proxy networks, and CAPTCHA solvers. 

1. Create an account at [app.skyvern.com](https://app.skyvern.com). 
2. Enter a prompt into the no-code workflow builder to start your first automation. 

> **Sample Prompt**:  
> "*Go to Amazon.com and add an iPhone 16, a case, and a screen protector to the cart*". 

### Local Installation

For developers who want to self-host or integrate directly into their local environment. 

#### Option A: pip install (Recommended)

For this option, you'll need [Python 3.11.x](https://www.python.org/downloads/) (3.12 is supported; 3.13 is not yet ready) and [NodeJS & NPM](https://nodejs.org/en/download/). Windows users will need [Rust](https://rustup.rs/) and [VS Code](https://code.visualstudio.com/) with C++ dev tools and Windows SDK to handle specific dependency compilations. 

##### 1. Install Skyvern

```bash
pip install skyvern
```

##### 2. Run Skyvern

```bash 
skyvern quickstart
```

If you already have a database you want to use, pass a custom connection string to skip the
local Docker PostgreSQL setup:

```bash
skyvern quickstart --database-string "postgresql+psycopg://user:password@localhost:5432/skyvern"
```

#### Option B: Docker Compose

##### 1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)

##### 2. Clone the repository:

   ```bash
   git clone https://github.com/skyvern-ai/skyvern.git && cd skyvern
   ```

##### 3. Run quickstart with Docker Compose:

   ```bash
   pip install skyvern && skyvern quickstart
   ```

##### 4. When prompted, choose "Docker Compose" for the full containerized setup.

##### 5. Navigate to http://localhost:8080

### Quick Start Examples

#### Run Via UI
```bash
skyvern run all
```
Navigate to http://localhost:8080 to run tasks through the web interface.

#### Python SDK 

```python
from skyvern import Skyvern

# Local mode
skyvern = Skyvern.local()

# Or connect to Skyvern Cloud
skyvern = Skyvern(api_key="your-api-key")

# Launch browser and get page
browser = await skyvern.launch_cloud_browser()
page = await browser.get_working_page()

# Mix Playwright with AI-powered actions
await page.goto("https://example.com")
await page.click("#login-button")  # Traditional Playwright
await page.agent.login(credential_type="skyvern", credential_id="cred_123")  # AI login
await page.click(prompt="Add first item to cart")  # AI-augmented click
await page.agent.run_task("Complete checkout with: John Snow, 12345")  # AI task
```

#### Typescript SDK

```typescript
import { Skyvern } from "@skyvern/client";

const skyvern = new Skyvern({ apiKey: "your-api-key" });
const browser = await skyvern.launchCloudBrowser();
const page = await browser.getWorkingPage();

// Mix Playwright with AI-powered actions
await page.goto("https://example.com");
await page.click("#login-button");  // Traditional Playwright
await page.agent.login("skyvern", { credentialId: "cred_123" });  // AI login
await page.click({ prompt: "Add first item to cart" });  // AI-augmented click
await page.agent.runTask("Complete checkout with: John Snow, 12345");  // AI task

await browser.close();
```

#### Simple Task Execution

```python
from skyvern import Skyvern

skyvern = Skyvern()
task = await skyvern.run_task(prompt="Find the top post on hackernews today")
print(task)
```

## SDK Quick Reference
 
Skyvern extends Playwright with AI-powered commands. Use selectors, natural language, or both:
 
```python
await page.click("#submit-button")                          # Traditional Playwright
await page.click(prompt="Click the green Submit button")    # AI-powered
await page.click("#submit-btn", prompt="Click Submit")      # AI fallback
```
 
### Page Commands
 
| Command | Description |
|---------|-------------|
| `page.act(prompt)` | Perform actions using natural language |
| `page.extract(prompt, schema)` | Extract structured data with optional JSON schema |
| `page.validate(prompt)` | Validate page state, returns `bool` |
| `page.prompt(prompt, schema)` | Send arbitrary prompts to the LLM with optional response schema |
 
### Agent Commands
 
| Command | Description |
|---------|-------------|
| `page.agent.run_task(prompt)` | Execute complex multi-step tasks |
| `page.agent.login(credential_type, credential_id)` | Authenticate with stored credentials (Skyvern, Bitwarden, 1Password) |
| `page.agent.download_files(prompt)` | Navigate and download files |
| `page.agent.run_workflow(workflow_id)` | Execute pre-built workflows |
 
### AI-Augmented Playwright Actions
 
| Action | Playwright | AI-Augmented |
|--------|------------|--------------|
| Click | `page.click("#btn")` | `page.click(prompt="Click login button")` |
| Fill | `page.fill("#email", "a@b.com")` | `page.fill(prompt="Email field", value="a@b.com")` |
| Select | `page.select_option("#country", "US")` | `page.select_option(prompt="Country dropdown", value="US")` |
| Upload | `page.upload_file("#file", "doc.pdf")` | `page.upload_file(prompt="Upload area", files="doc.pdf")` |
 
For full SDK documentation including all available methods and schema definitions, see the [docs](https://www.skyvern.com/docs).


## Supported LLMs

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


## Reliability & Safety

### Authentication & Security

- Supports username/password, session tokens, OAuth, and 2FA
- Stores sensitive credentials in encrypted vaults
- Self-hosted deployment options for full control

### Compliance

Skyvern is SOC 2 Type II compliant and configurable for HIPAA and GDPR compliance. For full details, see our [trust page](https://trust.skyvern.com).

### Self-Healing & Maintenance

- Self-healing automations that adapt to website changes
- Intelligent handling of CAPTCHAs, rate limits, and anti-bot protections
- Monitoring and reporting with logs, screenshots, execution history, and a livestream of the viewport that you can directly take control of

## Documentation and Resources

Extensive documentation can be found on our [docs page](https://www.skyvern.com/docs). Please let us know if something is unclear or missing by opening an issue or reaching out to us [via email](mailto:founders@skyvern.com) or [Discord](https://discord.gg/fG2XXEuQX3).

### Additional Resources 

- [Technical Evaluation](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/)
- [Model Context Protocol](https://github.com/Skyvern-AI/skyvern/blob/main/integrations/mcp/README.md)
- [2FA Support](https://www.skyvern.com/docs/credentials/totp)

### Integration Support 

- [Zapier](https://www.skyvern.com/docs/integrations/zapier)
- [Make.com](https://www.skyvern.com/docs/integrations/make.com)
- [N8N](https://www.skyvern.com/docs/integrations/n8n)


## Contributing

We welcome PRs and suggestions. Get started by chatting with the codebase via [Code Sage](https://sage.storia.ai?utm_source=github&utm_medium=referral&utm_campaign=skyvern-readme) to get a high-level overview of the repository structure, then submit a PR after looking at our [contribution guide](CONTRIBUTING.md) and our ["Help Wanted"](https://github.com/skyvern-ai/skyvern/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22) issues.

### Contributor Setup

Make sure to have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed.

1. Create your virtual environment:
  ```bash
      uv sync --group dev
  ```
2. Perform initial server configuration:
  ```bash
      uv run skyvern quickstart
  ```
3. Navigate to `http://localhost:8080` to start using the UI.

*The Skyvern CLI supports Windows, WSL, macOS, and Linux environments.*

Reach out [via email](mailto:founders@skyvern.com) or [Discord](https://discord.gg/fG2XXEuQX3) with questions or ideas.

## Community and Support

Interested in joining the Skyvern community to ask questions, share your workflows, or suggest features? 

- Follow us on [LinkedIn](https://www.linkedin.com/company/95726232)!
- Follow us on [X](https://twitter.com/skyvernai)!
- Reach out to us [via email](mailto:founders@skyvern.com)!
- Join our [Discord](https://discord.gg/fG2XXEuQX3) server!

## License
Skyvern's open source repository is supported via a managed cloud. All of the core logic powering Skyvern is available in this open source repository licensed under the [AGPL-3.0 License](LICENSE), with the exception of anti-bot measures available in our managed cloud offering.

If you have any questions or concerns around licensing, please [contact us](mailto:support@skyvern.com) and we'll be happy to help.

## Telemetry

By default, Skyvern collects basic usage statistics to help us understand how Skyvern is being used. If you would like to opt out of telemetry, please set the `SKYVERN_TELEMETRY` environment variable to `false`.