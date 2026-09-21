---
title: "Skyvern Changelog — June 2026"
description: null
excerpt: "Everything we shipped in June 2026 — weeks of June 1, 8, 15, and 22.\n\n\nWorkflows Are Now Agents\n\nWe renamed Workflows to Agents across the product, the public API, and the docs. The API now lives under /agents (e.g. POST /v1/run/agents) and accepts an agent_id alias for workflow_id — same wpid_ value, echoed back in responses. It's fully backwards-compatible: your existing workflow_id inputs, /workflows endpoints, SDK methods, and bookmarked doc URLs all keep working.\n\n\nWorkflow Studio (Beta)\n\nM"
slug: "skyvern-changelog-june-2026"
publicationState: "published"
publishedAt: "2026-07-01T12:57:25.000Z"
updatedAt: "2026-07-01T12:57:24.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/eeb3390f5ea43eb78215856eaf179e9c3a69eb3ee880d453500956416a7c0e5f-2026-06-00-header.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Everything we shipped in June 2026 — weeks of June 1, 8, 15, and 22.\n\n\nWorkflows Are Now Agents\n\nWe renamed Workflows to Agents across the product, the public API, and the docs. The API now lives under /agents (e.g. POST /v1/run/agents) and accepts an agent_id alias"
---
_Everything we shipped in June 2026 — weeks of June 1, 8, 15, and 22._

* * *



<h2 id="workflows-are-now-agents">Workflows Are Now Agents</h2>



We renamed **Workflows to Agents** across the product, the public API, and the docs. The API now lives under `/agents` (e.g. `POST /v1/run/agents`) and accepts an `agent_id` alias for `workflow_id` — same `wpid_` value, echoed back in responses. It's fully backwards-compatible: your existing `workflow_id` inputs, `/workflows` endpoints, SDK methods, and bookmarked doc URLs all keep working.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d1f20e0206d419bb530d1adfe451e0edbfa8aa6ac4d1649a091bfe248174805d-2026-06-01-agents-rename-1.png" class="kg-image" alt="Workflows are now Agents — the app renamed to Agents with the public API served under /agents and an agent_id alias for workflow_id, shown as backwards-compatible" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/9746ea00865a4bd81c3eb07fe1e7586ecd4dc51536527404e0fb534d19faa242-2026-06-01-agents-rename-1.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/0d6386d83e64f4f667273180aeb585a8df94763bb2c996d1470213d865335656-2026-06-01-agents-rename-1.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/d1f20e0206d419bb530d1adfe451e0edbfa8aa6ac4d1649a091bfe248174805d-2026-06-01-agents-rename-1.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="workflow-studio-beta">Workflow Studio (Beta)</h2>



Meet the next-generation editor. **Workflow Studio** is an opt-in preview that reimagines how you build: a refreshed **build canvas**, an **integrated live browser view**, and a built-in **run viewer** — all in one place. Take control of the live browser, center the viewport, and watch a run unfold without leaving the editor. Flip it on from Feature Preview in Settings.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/50d2f8b7df0f87c5cb01c5ae9709a3d54837024d7d0908eac56cde76d08b46d2-2026-06-02-workflow-studio.png" class="kg-image" alt="Workflow Studio preview — a redesigned editor with a build canvas on the left, an integrated live browser view in the center, and a run viewer panel, with a take-control button on the live browser" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/519e0f5900466f645fe7d56445aac4586323cd89a7db17ec40fe0e2f9ed2dd83-2026-06-02-workflow-studio.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/9690ce64c47f4032a76023f8031a529d14c4af85f10c944923eb5093a41ffcb2-2026-06-02-workflow-studio.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/50d2f8b7df0f87c5cb01c5ae9709a3d54837024d7d0908eac56cde76d08b46d2-2026-06-02-workflow-studio.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="code-first-code-blocks-beta">Code-First Code Blocks (Beta)</h2>



For when you want to drop to code. Code blocks now have a **dedicated code editor** with **syntax highlighting**, **Jinja parameter hints**, and a **run timeline** that records every action — including `page.evaluate(...)` calls. A **Build ↔ Code toggle** flips between visual and code editing, block labels and extraction output render correctly, and Copilot-generated code blocks support a **typed extraction schema** and fill logins with your stored credentials.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/88bed29aa82f98be3daafc567f7a55ce734b202fbf257260c181868ee3d34efe-2026-06-03-code-first.png" class="kg-image" alt="Code-first code block — a dedicated code editor with syntax highlighting and Jinja parameter hints, a Build/Code toggle in the header, and a run timeline listing each action taken" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/01aacea11bf4847f906c264758fb4b2f8f2caf63cfe71d88cccb446228392ac3-2026-06-03-code-first.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/c587520e9a4b2098b844ec008552c1993ec51468e50e32d406eb027a21534066-2026-06-03-code-first.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/88bed29aa82f98be3daafc567f7a55ce734b202fbf257260c181868ee3d34efe-2026-06-03-code-first.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="a-bigger-model-menu">A Bigger Model Menu</h2>



More choice for every run. New this month: **DeepSeek**, **Xiaomi MiMo** (and **MiMo-V2.5** via OpenRouter), **Gemini 3.5 Flash**, **GPT-5-mini** (your own OpenAI key or via OpenRouter), and **Claude Opus 4.8** (Anthropic + Bedrock). OpenRouter runs now track token counts and cost like every other provider. (Gemini 2.5 Pro/Flash are deprecated; CUA and the Claude Opus family are now enterprise-only.)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/b0e0fbdde6abefd8304133e1fc9001b63356e209d9ad80a9fd1b8b54a485ca65-2026-06-05-models.png" class="kg-image" alt="Model dropdown — an expanded model picker listing DeepSeek, Xiaomi MiMo, Gemini 3.5 Flash, GPT-5-mini, and Claude Opus 4.8, with an OpenRouter cost-tracking badge" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/9b02a99ea6c02301056936cc01de84b2c9d4b9a6a3217da7f60fffe012318086-2026-06-05-models.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/736d577bdeeaef431911e1e80443263d474e2a21ef3223730106db86bcd95128-2026-06-05-models.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/b0e0fbdde6abefd8304133e1fc9001b63356e209d9ad80a9fd1b8b54a485ca65-2026-06-05-models.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="credentials-otp-everywhere">Credentials &amp; OTP, Everywhere</h2>



Logins that just work. Pull credentials straight from **1Password** (with an item picker) or **Bitwarden**, look up one-time passcodes from **Gmail** via OAuth, and **fill OTP at runtime** from your stored TOTP secrets — even inside code blocks. Expired credential sessions **auto re-save**, and you can **clear a saved credential** without deleting and recreating it.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/420807bc232baddb7fbec9fabbd91c05318e25ded7f4a941c1d76255b46724b6-2026-06-06-credentials.png" class="kg-image" alt="Credentials setup — a credential picker offering 1Password and Bitwarden vault items, a Gmail-backed OTP lookup option, and a runtime OTP fill using a stored TOTP secret" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/a5cef054247c4ea811d62a5aaed6aca279a681d06408afeb5b870c80288b7af6-2026-06-06-credentials.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/9d4b6bd4eda8d7f2b68b4f83c73ae5515bbe8df21c2854d19afe12f395c96408-2026-06-06-credentials.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/420807bc232baddb7fbec9fabbd91c05318e25ded7f4a941c1d76255b46724b6-2026-06-06-credentials.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="multi-tab-browser-control">Multi-Tab Browser Control</h2>



Automations that span tabs. The autonomous agent loop can now **open and work across multiple browser tabs** at once — following new-tab links and handling pop-ups — so flows that jump between tabs no longer dead-end.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/1f14587dd333d06b1b9b5fd80e2ecd5eb0379d3e9946897cef01c16602be6e7c-2026-06-07-multi-tab.png" class="kg-image" alt="Multi-tab browser control — an agent working across two browser tabs at once, following a link that opened a second tab and acting in both" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/e4dc88d39676ff150fc28c8e075fcc2ec77261cfeb3f75f05272f2d320bcebe6-2026-06-07-multi-tab.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/e635e51928bafad37667aa5a06146b1b688550d651881399635197468caf1cdf-2026-06-07-multi-tab.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/1f14587dd333d06b1b9b5fd80e2ecd5eb0379d3e9946897cef01c16602be6e7c-2026-06-07-multi-tab.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="self-host-on-google-cloud">Self-Host on Google Cloud</h2>



Full GCP-native deployments. Self-hosted Skyvern now supports **Google Cloud Storage** for artifacts and **GCP Secret Manager** as a vault backend — so you can run the whole stack on Google Cloud with your own storage and secrets.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/a38aa2d7706e1c05113ca8433a117959feeccc4d0b19efee8b4c10ff0c237c5a-2026-06-08-gcp-self-host.png" class="kg-image" alt="Self-hosting on GCP — a deployment config showing Google Cloud Storage selected for artifacts and GCP Secret Manager selected as the vault backend" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/6b694e8f672e8e1851688ccea15f4882d73d7574aad33cf27882ff6170d1f0db-2026-06-08-gcp-self-host.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/f254850f15241528910ceced606ca00c8b7ba945b4a5de5754d17da8a19e4f33-2026-06-08-gcp-self-host.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/a38aa2d7706e1c05113ca8433a117959feeccc4d0b19efee8b4c10ff0c237c5a-2026-06-08-gcp-self-host.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="quick-list-%E2%80%94-june">Quick List — June</h2>



**New features**

-   <strong>Analytics drill-down &amp; tag filters</strong> — period-aware per-agent drill-down (avg duration, credits/run, period-over-period trends) plus filtering runs and metrics by workflow tag
-   <strong>Agent Directory Tree</strong> — the agents/folders list is now a navigable tree for nested hierarchies
-   <strong>Self-serve editor onboarding</strong> — a get-started modal, a 4-stop guided tour, and smart empty states for faster time-to-first-automation
-   <strong>Bulk actions on the agents list</strong> — select multiple agents and act on them at once
-   <strong>Label management in Settings</strong> — rename, recolor, and soft-delete tags
-   <strong>Browser profiles in MCP sessions</strong> — associate a browser profile with MCP-driven runs for credential/state reuse
-   <strong>Feature Preview panel in Settings</strong> — opt into upcoming features before they're fully released
-   <strong>Workflow schedules in open source</strong> — self-hosted deployments now support schedules
-   <strong>Uncapped workflow runtime</strong> — configure long-running automations with no maximum time limit

**Improvements**

-   Structured output is verified against its schema before a run is marked complete
-   Tasks return a best-effort <strong>partial deliverable</strong> when they exhaust their credit budget
-   The task planner prefers <strong>loop-based tasks</strong> for repeated same-shaped work, and can now emit EXTRACT\_INFORMATION / GOTO\_URL actions
-   The agent <strong>acts immediately on stalled pages</strong> and **halts reliably on terminal blockers** instead of spinning
-   Remote browser sessions capped at 4 hours; debug sessions stay open after a run so you can inspect the final page
-   End-state screenshots captured for <strong>every open tab</strong> at completion; per-run recordings clipped to the run window and stored as downloadable artifacts
-   Run app + recording URLs surfaced in MCP and CLI responses; run-ID search fallback on the runs list
-   Batched cache invalidation on workflow save (removed a per-block N+1) for faster saves on large workflows
-   OpenRouter cost tracking; analytics tag filters accept labels, groups, or `group:label`
-   Improved authenticator-2FA credential setup; PDF field mapping by positional labels
-   Copilot data-write blocks default to `continue_on_failure=false`; code-repair progress shown as quiet indicators

**Bug fixes**

-   Fixed the workflow builder freezing on an infinite FlowRenderer layout loop
-   Fixed the code editor crashing (stack overflow) on large or deeply-nested JSON
-   Fixed the app failing to load after a deploy from stale chunks — it now auto-recovers with a cache-busting reload
-   Fixed login credentials not reattaching after editing a workflow via MCP
-   Fixed conditional routing not being preserved when loading a v1-format workflow
-   Fixed the active run context being lost when switching organizations
-   Fixed multi-step logins not advancing when credentials filled JavaScript-gated fields
-   Fixed scanned PDFs being parsed as a single page instead of page-by-page
-   Fixed shadow-DOM text nodes being missed during element text extraction
-   Fixed runs not stopping when a configured time limit was exceeded
-   Fixed credits not recorded when data was captured inside a Navigate block
-   Fixed telephone inputs dropping digits on bare NANP-format numbers
-   Fixed persistent sessions reporting a stale cached session as live
-   Fixed FileUpload blocks silently skipping when no file was provided (now a clean no-op)
-   Fixed the block library drawer overflowing its container in the workflow builder
