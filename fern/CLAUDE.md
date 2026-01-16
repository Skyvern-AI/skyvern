# Skyvern Documentation Revamp — Agent Context Guide

This file contains all context, decisions, and methodology for the Skyvern documentation revamp project. Any AI agent working on this documentation should read this file first.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Product Understanding](#product-understanding)
3. [Customer Personas](#customer-personas)
4. [Research Methodology](#research-methodology)
5. [Information Architecture Decisions](#information-architecture-decisions)
6. [Tab Structure & Rationale](#tab-structure--rationale)
7. [Key Documentation Gaps Identified](#key-documentation-gaps-identified)
8. [Support Ticket Insights](#support-ticket-insights)
9. [How to Get Context](#how-to-get-context)
10. [Writing Guidelines](#writing-guidelines)

---

## Project Overview

### What is Skyvern?

Skyvern is an **AI-powered browser automation platform** that uses computer vision and LLMs to interact with websites without requiring brittle XPath/CSS selectors. Unlike traditional automation tools (Selenium, Playwright, Puppeteer), Skyvern can adapt to UI changes because it "sees" the page like a human.

### Core Value Proposition

> "Automate any website workflow without writing selectors. Skyvern's AI adapts to UI changes automatically."

### The "Aha Moment"

Users realize Skyvern's value when they:
1. See their first task complete successfully on a complex site
2. Watch the same automation work after the target site's UI changed
3. Successfully automate a site with CAPTCHA/bot detection that blocked traditional tools

**Documentation goal**: Get users to this moment as fast as possible.

---

## Product Understanding

### Three Ways to Use Skyvern

| Mode | What It Is | Who Uses It | Key Consideration |
|------|-----------|-------------|-------------------|
| **Skyvern Cloud** | Fully managed browser infrastructure | Most users, production workloads | Easiest, most reliable |
| **Local Browser** | Your own browser, Skyvern's AI | Debugging, development | Browser pops up locally, you watch it |
| **Self-Hosted** | Full deployment on your infrastructure | Enterprise, data-sensitive orgs | Docker Compose, requires DevOps |

### Product Components

#### Tasks (Atomic Unit)
- Single browser automation job
- Has: URL, prompt, optional data extraction schema
- Returns: extracted data, screenshots, recording URL
- Can run on different "engines" (AI models)

#### Workflows (Orchestration Layer)
- Chain multiple steps together
- 21 block types for different operations
- Visual builder in Cloud UI OR YAML definition via API
- Supports: loops, conditionals, human-in-the-loop, file operations

#### Workflow Blocks (21 Types)
```
Navigation:        TaskBlock, NavigationBlock, ActionBlock
Control Flow:      ForLoopBlock, LoopBlock, ConditionalBlock, ValidationBlock
Data:              TextPromptBlock, ExtractionBlock, DataExtractionBlock
Files:             FileDownloadBlock, UploadToS3Block, FileURLParserBlock, PDFParserBlock
Communication:     SendEmailBlock, WebhookBlock
Code:              CodeBlock
Interaction:       WaitBlock, ManualTaskBlock, LoginBlock
```

#### Browser Sessions
- Persistent browser instances
- Can be created, reused, and managed
- Useful for: maintaining login state, multi-step flows

#### Browser Profiles
- Store cookies, localStorage, session data
- Persist authentication across runs
- Key for: avoiding re-login, maintaining state

#### Credentials
- Secure storage for passwords, API keys, credit cards
- Types: `password`, `api_key`, `credit_card`, `2fa_totp`
- Referenced in workflows, never exposed in logs

#### Proxies
- Route traffic through different IPs
- Types: Residential, Datacenter, ISP
- 60+ global locations
- Critical for: bot detection avoidance, geo-restricted content

#### AI Engines
| Engine | Best For |
|--------|----------|
| `skyvern-1.0` | Simple, well-structured sites |
| `skyvern-2.0` | Complex sites, recommended default |
| `openai-cua` | OpenAI's Computer Use Agent |
| `anthropic-cua` | Anthropic's Computer Use |
| `ui-tars` | UI-TARS model |

---

## Customer Personas

### 1. Integration Developer
- **Role**: Software engineer building automations
- **Uses**: API/SDK, code-first approach
- **Needs**: Clear API reference, code examples, SDK docs
- **Pain points**: Parameter confusion, error handling, async patterns

### 2. Automation Operator  
- **Role**: Non-technical or low-code user
- **Uses**: Cloud UI (visual workflow builder)
- **Needs**: UI guides, templates, step-by-step tutorials
- **Pain points**: Understanding blocks, debugging failures

### 3. Decision Maker
- **Role**: Engineering manager, C-suite
- **Uses**: Overview, pricing, security docs
- **Needs**: ROI justification, compliance info, case studies
- **Pain points**: Understanding capabilities vs. limitations

### Persona → Product Mapping

```
Integration Developer → Documentation Tab, SDK Reference, API Reference
Automation Operator  → Skyvern Cloud (UI) Tab, Cookbooks
Decision Maker       → Introduction, Overview, Use Cases
```

---

## Research Methodology

### Phase 1: Product Discovery
1. Read OpenAPI spec (`fern/openapi/skyvern_openapi.json`)
2. Extracted all endpoints, schemas, enums
3. Identified 21 workflow blocks, 5 AI engines, 60+ proxy locations
4. Installed SDK locally, inspected method signatures

### Phase 2: Documentation Audit
1. Mapped current structure from `fern/docs.yml`
2. Read every existing `.mdx` file
3. Identified hidden vs. visible pages
4. Found critical gap: `workflow-blocks.mdx` (hidden, 242 lines, detailed) vs. `workflow-blocks-details.mdx` (visible, 57 lines, thin)

### Phase 3: Support Ticket Analysis
1. Processed 408 Pylon tickets (Dec 2024 - Jan 2025)
2. Built Python script with Claude API for classification
3. Categories: Setup, Tasks, Workflows, Errors, Features, Billing, Enterprise

**Key Stats from Tickets:**
- 38% Tasks & Execution issues
- 19% Workflow & Block Configuration
- 15% CAPTCHA/Bot Detection (biggest pain point)
- 25% from Finance/Insurance sector

### Phase 4: Competitor Analysis
For cookbooks, reviewed:
- OpenAI Cookbook
- Algolia documentation
- Modular GenAI Cookbook
- Anthropic documentation patterns

---

## Information Architecture Decisions

### Why Tab-Based Structure?

**Problem**: Previous docs mixed API users, UI users, and decision makers in one linear flow.

**Solution**: Separate tabs for different audiences:
- `Documentation` → Developers using API/SDK
- `Skyvern Cloud (UI)` → Visual builder users
- `SDK Reference` → Technical reference
- `API Reference` → Auto-generated from OpenAPI
- `Cookbooks` → End-to-end tutorials

### Why Outcomes-Based Organization?

**Problem**: Original IA was feature-focused ("here's what Tasks do").

**Solution**: Organize by what users want to achieve:
- "Extract data from websites" instead of "Use ExtractionBlock"
- "Handle login flows" instead of "LoginBlock configuration"
- "Debug failed automations" instead of "Error codes"

### Why Core Concepts Early?

Users kept asking basic questions in support:
- "What's the difference between Task and Workflow?"
- "When do I use a block vs. a task?"
- "What are browser profiles for?"

**Decision**: Add explicit Core Concepts section after quickstart.

---

## Tab Structure & Rationale

### Tab 1: Documentation (API/SDK Users)

```
Documentation
├── Introduction
│   - What Skyvern is, value proposition
│   - When to use Skyvern vs. alternatives
│
├── Quickstart  
│   - 5-minute first automation
│   - Covers Cloud vs. Local browser choice
│
├── Core Concepts
│   ├── Tasks vs. Workflows
│   ├── AI Engines
│   ├── Browser Sessions & Profiles
│   └── Credentials & Security
│
├── Running Tasks
│   ├── Basic Task Execution
│   ├── Data Extraction
│   ├── Navigation & Actions
│   └── File Operations
│
├── Building Workflows
│   ├── Workflow Basics
│   ├── Block Reference (all 21 blocks)
│   ├── Control Flow (loops, conditionals)
│   ├── Data Passing Between Blocks
│   └── Human-in-the-Loop
│
├── Advanced Features
│   ├── Webhooks & Callbacks
│   ├── Proxy Configuration
│   ├── Bot Detection Handling
│   ├── Caching & Optimization
│   └── Keyboard & Mouse Actions
│
├── Debugging & Troubleshooting
│   ├── Reading Logs & Artifacts
│   ├── Common Errors
│   ├── CAPTCHA Troubleshooting
│   └── Login Failures
│
└── Self-Hosting
    ├── Docker Compose Setup
    ├── Environment Variables
    ├── Database Configuration
    └── Scaling & Production
```

**Rationale**: Linear progression from "what is this" → "quick win" → "understand concepts" → "do more" → "debug problems" → "run yourself"

### Tab 2: Skyvern Cloud (UI)

```
Skyvern Cloud (UI)
├── Getting Started
│   ├── Account Setup
│   ├── Dashboard Overview
│   └── Your First Workflow (Visual)
│
├── Workflow Builder
│   ├── Creating Workflows
│   ├── Adding & Configuring Blocks
│   ├── Testing Workflows
│   └── Templates & Examples
│
├── Credentials Manager
│   ├── Adding Credentials
│   ├── Credential Types
│   └── Using in Workflows
│
├── Run History & Monitoring
│   ├── Viewing Runs
│   ├── Artifacts & Recordings
│   └── Failure Analysis
│
└── Settings & Organization
    ├── API Keys
    ├── Team Management
    └── Billing
```

**Rationale**: UI users need visual guides, not code. Separate their journey entirely.

### Tab 3: SDK Reference

```
SDK Reference
├── Installation
├── Client Initialization
├── Tasks
│   ├── run_task()
│   ├── get_task()
│   └── cancel_task()
├── Workflows
│   ├── run_workflow()
│   ├── get_workflow()
│   └── create_workflow()
├── Browser Sessions
├── Credentials
├── Webhooks
└── Error Handling
```

**Rationale**: Technical reference for developers who know what they want and need method signatures.

### Tab 4: API Reference
- Auto-generated from OpenAPI spec
- No manual maintenance needed

### Tab 5: Cookbooks

```
Cookbooks
├── Overview
├── Multi-Carrier Insurance Quote Aggregator
├── State Business Formation Bot
├── Healthcare Portal Data Extraction
├── Bulk Invoice Downloader with Email Summary
└── Job Application Pipeline
```

**Rationale**: End-to-end, copy-paste tutorials. Chosen based on:
- Support ticket frequency (insurance = 25% of tickets)
- Real customer use cases (GovAssist = government forms)
- Common pain points (Cloudflare, bot detection)

---

## Key Documentation Gaps Identified

### Critical (From Ticket Analysis)

| Gap | Evidence | Priority |
|-----|----------|----------|
| CAPTCHA/Bot Detection | 15+ tickets, no dedicated guide | P0 |
| Output parameter passing | Ticket #2206, undocumented | P0 |
| Browser profile usage | Tickets asking "how to persist login" | P0 |
| Queue system behavior | Users confused about parallel execution | P1 |
| File block data flow | "How to access downloaded file in next block" | P1 |

### High (From Documentation Audit)

| Gap | Issue |
|-----|-------|
| Hidden `workflow-blocks.mdx` | Best content is hidden, visible page is thin |
| Deprecated pages | `advanced-features.mdx` and `api-spec.mdx` should be removed |
| No Core Concepts page | Users lack mental model |
| No UI documentation | Entire Cloud UI undocumented |

### Medium (From Competitor Analysis)

| Gap | Competitor Reference |
|-----|---------------------|
| No cookbooks | OpenAI, Anthropic have extensive recipes |
| No migration guide | Users coming from Selenium/Playwright |
| No architecture diagram | How Skyvern works under the hood |

---

## Support Ticket Insights

### Top Issue Categories

1. **Tasks & Execution (38%)**
   - Data extraction failures
   - Navigation issues
   - Timeout problems

2. **Workflows (19%)**
   - Block configuration
   - Parameter passing
   - Loop behavior

3. **Bot Detection (15%)**
   - Cloudflare blocks
   - CAPTCHA failures
   - 403 errors

4. **Setup (12%)**
   - API key issues
   - SDK installation
   - Environment configuration

### Code Patterns from Tickets

These patterns appeared frequently and should be in docs:

```python
# Pattern: Polling for task completion
while True:
    result = await client.get_task(task_id)
    if result.status in ["completed", "failed"]:
        break
    await asyncio.sleep(5)

# Pattern: Residential proxy for bot detection
await client.run_task(
    url="https://protected-site.com",
    proxy_location="RESIDENTIAL_US_CA"
)

# Pattern: Browser profile for persistent login
profile = await client.create_browser_profile()
await client.run_task(url="...", browser_profile_id=profile.id)
```

### Feature Requests from Tickets

1. Better error messages (most requested)
2. Webhook retry configuration
3. Custom wait conditions
4. Screenshot at specific steps
5. Headless mode for local browser

---

## How to Get Context

### For Product Understanding

1. **OpenAPI Spec**: `/fern/openapi/skyvern_openapi.json`
   - All endpoints, schemas, enums
   - Definitive source for API capabilities

2. **SDK Source**: Install `skyvern` package, inspect with:
   ```python
   from skyvern import Skyvern
   import inspect
   print(inspect.signature(Skyvern.run_task))
   ```

3. **Existing Docs**: `/fern/` directory, read `.mdx` files

### For Customer Context

1. **Ticket Analysis**: `/Users/namanbansal/skyvern-tests/ticket_analysis/ticket_analysis_report.md`
   - 2048 lines of analyzed support data
   - Common issues, code patterns, feature requests

2. **Context Document**: `/Users/namanbansal/skyvern-docs/skyvern-ia-context.md`
   - 654 lines of product and persona research

### For IA Decisions

1. **This file** (`CLAUDE.md`)
2. **Gap Analysis**: `/Users/namanbansal/skyvern-tests/ticket_analysis/ia_gap_analysis.md`

---

## Writing Guidelines

### Voice & Tone

- **Clear**: No jargon without explanation
- **Practical**: Lead with code, explain after
- **Honest**: State limitations, don't oversell

### Code Examples

- Always runnable, copy-paste ready
- Include imports and setup
- Show expected output
- Handle errors appropriately

### Structure

- Start with what the user wants to achieve
- Show the simplest working example first
- Add complexity incrementally
- End with common gotchas/troubleshooting

### What NOT to Do

- Don't duplicate content across pages
- Don't assume prior knowledge of Skyvern
- Don't hide important features in footnotes
- Don't use "simply" or "just" (things aren't simple for beginners)

---

## Files Reference

| File | Purpose |
|------|---------|
| `/fern/docs.yml` | Documentation structure config |
| `/fern/openapi/skyvern_openapi.json` | API specification |
| `/fern/**/*.mdx` | Documentation pages |
| `/fern/images/` | Screenshots and diagrams |
| `skyvern-ia-context.md` | Product research document |
| `skyvern-api-features.html` | API feature mapping |

---

## Changelog

| Date | Change |
|------|--------|
| 2026-01-14 | Initial CLAUDE.md created with full context from docs revamp |

---

## Questions to Ask Before Writing

1. Who is the audience for this page? (Developer, Operator, Decision Maker)
2. What does the user want to achieve? (Not: what feature are we documenting)
3. What's the simplest working example?
4. What errors will they hit? Document preemptively.
5. Is this covered elsewhere? Link, don't duplicate.

---

*This file should be updated as the documentation evolves.*
