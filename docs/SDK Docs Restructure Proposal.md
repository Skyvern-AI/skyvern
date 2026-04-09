# Proposal: SDK & Developer Docs Restructure

> Goal: Get developers from zero to running automation in < 5 minutes, then provide deep reference for every operation.

---

## Current State

The existing SDK docs (`sdk-reference/` and `ts-sdk-reference/`) are solid but have structural issues:

1. **Two completely separate sections** for Python and TS, with duplicated prose. Maintaining parity is a constant tax.
2. **Browser automation is TS-only in the docs** -- the Python SDK has an equally rich Playwright integration (`skyvern/library/`) that isn't documented at all: `page.act()`, `page.extract()`, `page.fill_form()`, `AILocator`, iframe management, etc.
3. **No unified "what can I do?" view** -- a developer can't quickly see all operations without clicking through 9-10 separate pages per SDK.
4. **The "Developers" tab** in the sidebar mixes conceptual guides (Running Automations, Handling Auth) with reference material. New users don't know where to start.
5. **Missing quick-start for the Playwright-style SDK** -- the fastest path to value (launch browser, AI-control it) is buried deep in one TS page.

---

## Proposed Structure

Reorganize the **Developers tab** and **SDK Reference tab** into a flow that mirrors how developers actually adopt the SDK:

```
Developers (tab)
├── Quickstart (5-min guide, both languages)
├── Core Concepts
│   ├── Tasks
│   ├── Workflows
│   ├── Browser Sessions
│   └── Engines & Models
├── Guides
│   ├── Authentication & Credentials
│   ├── Data Extraction
│   ├── File Downloads & Uploads
│   ├── Multi-Step Workflows
│   ├── Scheduling Automations
│   ├── Error Handling & Retries
│   ├── Webhooks & Callbacks
│   ├── Going to Production
│   └── Self-Hosted Deployment
├── Optimization
│   ├── Cached Scripts
│   └── Cost & Performance
└── Debugging
    ├── Run Timeline & Artifacts
    └── Troubleshooting

SDK Reference (tab)
├── Overview & Installation
│   ├── Python
│   └── TypeScript
├── Browser Automation            <-- NEW unified page
│   ├── Launching Browsers
│   ├── Page AI Actions (act, extract, validate, prompt, find)
│   ├── AI-Enhanced Playwright (click, fill, select_option)
│   ├── Form Automation (fill_form, fill_multipage_form, etc.)
│   ├── Agent on Page (run_task, login, download_files, run_workflow)
│   └── iframe Management
├── REST API Client
│   ├── Tasks
│   ├── Workflows
│   ├── Runs
│   ├── Browser Sessions
│   ├── Browser Profiles
│   ├── Credentials
│   ├── Folders
│   ├── Scripts
│   ├── Scheduling
│   ├── Artifacts
│   └── File Upload
├── Types & Enums
├── Error Handling
└── Complete Reference (all operations table)
```

---

## Key Changes

### 1. Unified quickstart (new page)

A single page with side-by-side Python/TS tabs. Three paths in order of complexity:

```
Path A: "I want to run a task" (3 lines of code)
  pip install skyvern / npm install @skyvern/client
  → run_task(prompt, url, wait_for_completion=True)
  → print result

Path B: "I want to control a browser with AI" (5 lines)
  → launch_cloud_browser()
  → page.goto(url)
  → page.act("Fill out the form")
  → page.extract({prompt, schema})
  → browser.close()

Path C: "I want a multi-step workflow" (link to workflow builder)
```

This replaces the current separate overview pages that both cover constructor params, environments, etc.

### 2. Browser Automation gets first-class treatment (new page)

This is the biggest gap. The Python SDK's `skyvern/library/` module provides:

- `page.act(prompt)`, `page.extract(prompt, schema)`, `page.validate(prompt)`
- `page.click(prompt=..., selector=...)` with AI fallback
- `page.fill_form(data)`, `page.fill_multipage_form(data)`, `page.fill_from_mapping(mapping)`
- `page.find(prompt)` returning an `AILocator` with full Playwright chaining
- `page.frame_switch()`, `page.frame_main()`, `page.frame_list()`
- `page.agent.run_task()`, `page.agent.login()`, etc.

None of this is documented today. The proposed "Browser Automation" page covers both SDKs with tabbed code examples and explains the three layers:

| Layer | What it does | Example |
|-------|-------------|---------|
| **Standard Playwright** | Direct browser control | `page.goto()`, `page.click("#btn")` |
| **AI-Enhanced Playwright** | Selector + AI fallback | `page.click(prompt="Click submit")` |
| **Pure AI Actions** | No selector needed | `page.act("Fill the form and submit")` |

### 3. Tabbed code blocks instead of duplicate pages

Every code example uses Mintlify's `<CodeGroup>` with Python and TypeScript tabs:

```
<CodeGroup>
```python
await page.act("Click the submit button")
```

```typescript
await page.act("Click the submit button");
```
</CodeGroup>
```

This eliminates the maintenance burden of keeping 9 Python pages and 10 TS pages in sync.

### 4. "Complete Reference" becomes a real operations table

Replace the current auto-generated reference pages with the comprehensive operations table from `sdk-operations-reference.md`. Developers scan this to answer "does the SDK support X?" in seconds.

### 5. Document Python SDK parity gaps explicitly

Call out what's missing so developers aren't surprised:

| Missing from Python SDK | Workaround |
|------------------------|------------|
| Scheduling (8 endpoints) | Use REST API directly or TS SDK |
| Artifact download (2 endpoints) | Use REST API directly |
| Script version management (6 endpoints) | Use REST API directly |
| Credential vault provider CRUD | Use REST API directly |

---

## Page-by-Page Spec

### New pages to write

| Page | Priority | Why |
|------|----------|-----|
| **Quickstart** (unified) | P0 | Current onboarding is split and slow |
| **Browser Automation** (unified) | P0 | Python's Playwright integration is undocumented |
| **Complete Operations Reference** | P1 | No single view of all SDK capabilities |
| **Form Automation** | P1 | `fill_form`, `fill_multipage_form`, `AILocator` are unique differentiators |
| **Types & Enums** | P2 | Currently scattered across reference pages |
| **Scheduling** | P2 | 8 TS endpoints, no docs page |

### Pages to update

| Page | Change |
|------|--------|
| Python Overview | Merge into unified overview with TS tabs |
| TS Overview | Merge into unified overview with Python tabs |
| Tasks (both) | Merge into single tabbed page |
| Workflows (both) | Merge into single tabbed page |
| Browser Sessions (both) | Merge into single tabbed page |
| Browser Profiles (both) | Merge into single tabbed page |
| Credentials (both) | Merge into single tabbed page, add vault provider CRUD |
| Helpers (both) | Merge into Browser Automation page (login, download_files) |
| Error Handling (both) | Merge into single tabbed page |

### Pages to remove (after merge)

All individual `sdk-reference/*.mdx` and `ts-sdk-reference/*.mdx` pages get replaced by the unified versions. Set up redirects.

---

## Implementation Plan

### Phase 1: Fill the biggest gap (1-2 days)

1. Write **Browser Automation** page covering both SDKs
2. Write **Quickstart** page with the 3 paths
3. Add `browser-automation` to Python SDK sidebar (it currently only exists for TS)

### Phase 2: Unify SDK reference (2-3 days)

4. Merge each pair of pages (tasks, workflows, etc.) into single tabbed pages
5. Write the **Complete Operations Reference** table page
6. Write **Types & Enums** page
7. Add **Scheduling** page (TS-only for now, note Python gap)

### Phase 3: Restructure navigation (1 day)

8. Reorganize `docs.json` sidebar to match proposed structure
9. Add redirects from old paths to new paths
10. Remove old individual pages

### Phase 4: Fill remaining gaps

11. Write **Form Automation** deep-dive (Python's `fill_form`, `fill_multipage_form`, `AILocator`)
12. Document WebSocket/streaming endpoints
13. Add interactive examples or embedded playground links

---

## Open Questions

1. **Do we keep separate `/sdk-reference/` and `/ts-sdk-reference/` URL prefixes?** Merging to a single `/sdk/` prefix with language tabs would be cleaner but requires more redirects.

2. **Should the Python SDK's form automation methods (`fill_form`, `fill_multipage_form`, `validate_mapping`) be added to the TS SDK?** They're a significant differentiator that should either be ported or clearly documented as Python-exclusive.

3. **Should we document the WebSocket streaming endpoints?** They power the live viewer in the UI but aren't commonly used by SDK consumers directly. Could be a low-priority "Advanced" page.

4. **What about the `Skyvern.local()` embedded mode?** It's powerful (in-memory SQLite, no infra needed) but only Python. Deserves its own section or page.
