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

<figure align="center">
<img src="fern/images/geico_shu_recording_cropped.gif"/>
<figcaption> Skyvern in action, navigating the Geico site to acquire an insurance quote.</figcaption>
</figure>

--- 

## Table of Contents 

- [Why Skyvern](#why-use-skyvern)
- [Use Cases](#what-can-i-use-skyvern-for)
- [How It Works](#how-does-it-work)
- [Quickstart](#quickstart)
- [Examples](#quick-start-examples)
- [Reliability and Safety](#reliabilty--safety)
- [Documentation](#documentation)
- [License](#license)
- [Telemetry Disclaimer](#telemetry)
- [Community & Contribution](#community-and-support)

>  **IMPORTANT**  
Complete technical documentation, build guides, and integration patterns are available in our [docs](https://www.skyvern.com/docs).


## Why Use Skyvern?

Instead of relying solely on code-defined interactions (like DOM parsing and XPaths), Skyvern uses prompts along with computer vision and large language models (LLMs) to parse items in the viewport in real-time, plan interactions, and execute them. 

Simply put, Skyvern can recognize the intent and goal of a page and respond to it accordingly, similar to how a human would. 

This lets Skyvern: 

- Operate on websites it's never seen before
- Continue operating even in the face of website layout changes
- Take a single workflow and apply it to a large number of websites

If you're wondering if Skyvern is for you, then **ask yourself these five questions:** 
- Do web pages frequently break your automation scripts? 
- Does maintaining browser automation take more time than the tasks themselves?
- Are you frustrated by constantly having to manually update selectors, paths, or form scripts? 
- Do site updates or UI changes cause cascading failures?
- Would an AI that understands pages like a human make your job easier?

If you answered "Yes" to any of the questions above, Skyvern will save you time and headaches. 


## What Can I Use Skyvern For?

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

> **Wanna see?**  
> Check out some of examples of how you can run Skyvern in production [here](https://www.skyvern.com/docs/getting-started/skyvern-in-action).  

## How Does It Work?

Skyvern uses prompts along with computer vision and LLMs to parse items in the viewport in real-time, plan interactions, and execute them.

It provides a Playwright-compatible SDK that adds AI functionality on top of Playwright, as well as a no-code workflow builder to help both technical and non-technical users automate manual workflows. 

> **Full SDK and API Documentation**  
> For a complete list of AI-augmented Playwright actions (like fill, select_option, and upload_file) and detailed schema definitions, see our [docs](https://www.docs.skyvern.com). 

<figure align="center">
  <source media="(prefers-color-scheme: dark)" srcset="fern/images/skyvern_2_0_system_diagram.png" />
  <img src="fern/images/skyvern_2_0_system_diagram.png" />
  <figcaption>Skyvern uses a swarm of agents to comprehend a website, plan, and execute its actions.  </figcaption>
</figure>

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

For this option, you'll need [Python 3.11.x](https://www.python.org/downloads/) (3.12 is supported; 3.13 is not yet ready) and [NodeJS & NPM](https://nodejs.org/en/download/). Windows users will need [Rust](https://rustup.rs/) and [VS Code] with C++ dev tools and Windows SDK to handle specific dependency compilations. 

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

## Quick Start Examples

**Run via UI:**
```bash
skyvern run all
```
Navigate to http://localhost:8080 to run tasks through the web interface.

**Python SDK:**
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

**TypeScript SDK:**
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

**Simple task execution:**
```python
from skyvern import Skyvern

skyvern = Skyvern()
task = await skyvern.run_task(prompt="Find the top post on hackernews today")
print(task)
```

## Reliabilty & Safety

### Authentication & Security

- Supports username/password, session tokens, OAuth, and 2FA
- Stores sensitive credentials in encrypted vaults
- SOC 2 Type II compliant
- Self-hosted deployment options for full control
- Configurable for HIPAA and GDPR compliance

### Maintenance

- Self-healing automations that adapt to website changes
- Intelligent handling of CAPTCHAs, rate limits, and anti-bot protections
- Monitoring & reporting with logs, screenshots, execution history, and a livestream of the viewport that you can directly take control of.

## Documentation

Extensive documentation can be found on our [docs page](https://www.skyvern.com/docs). Please let us know if something is unclear or missing by opening an issue or reaching out to us [via email](mailto:founders@skyvern.com) or [discord](https://discord.gg/fG2XXEuQX3).

### Addtional Resources 

- [Technical Evaluation](https://www.skyvern.com/blog/skyvern-2-0-state-of-the-art-web-navigation-with-85-8-on-webvoyager-eval/).
- [Model Context Protocol](https://github.com/Skyvern-AI/skyvern/blob/main/integrations/mcp/README.md)
- [2FA Support](https://www.skyvern.com/docs/credentials/totp)

### Integration Support 

* [Zapier](https://www.skyvern.com/docs/integrations/zapier)
* [Make.com](https://www.skyvern.com/docs/integrations/make.com)
* [N8N](https://www.skyvern.com/docs/integrations/n8n)


## License
Skyvern's open source repository is supported via a managed cloud. All of the core logic powering Skyvern is available in this open source repository licensed under the [AGPL-3.0 License](LICENSE), with the exception of anti-bot measures available in our managed cloud offering.

If you have any questions or concerns around licensing, please [contact us](mailto:support@skyvern.com) and we'll be happy to help.

## Telemetry

By Default, Skyvern collects basic usage statistics to help us understand how Skyvern is being used. If you would like to opt-out of telemetry, please set the `SKYVERN_TELEMETRY` environment variable to `false`.

## Community and Contribution

Interested in joining the Skyvern community to ask questions, share your workflows, suggest features, or contribute to development? 

- Follow us on [LinkedIn](https://www.linkedin.com/company/95726232)!
- Follow us on [X](https://twitter.com/skyvernai)!
- Reach out to us [via email](mailto:founders@skyvern.com)!
- Join our [Discord](https://discord.gg/fG2XXEuQX3) server!
- Submit a PR after looking at our [contribution guide](CONTRIBUTING.md) and our ["Help Wanted"](https://github.com/skyvern-ai/skyvern/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22) issues!
- Chat with the Skyvern repository via [Code Sage](https://sage.storia.ai?utm_source=github&utm_medium=referral&utm_campaign=skyvern-readme)!