---
title: "Skyvern Changelog — April & May 2026"
description: null
excerpt: "Two months of shipping in one email — a draggable editor, cleaner MCP OAuth, stronger run controls, and plenty more. Headliners first, the full list at the end.\n\n\nApril 2026\n\n\nWorkflow Copilot v2\n\nThe workflow copilot got a lot more hands-on. Hard-cancel a run mid-flight instead of waiting it out, and follow it live as it streams status narration and surfaces block-level events (starts, completions, errors) while your workflow runs. When a test trips, it hands back the work-in-progress workflow "
slug: "skyvern-changelog-april-may-2026"
publicationState: "published"
publishedAt: "2026-06-05T20:24:12.000Z"
updatedAt: "2026-06-05T20:24:12.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/646a9c6cb70118b8727c3c1679b41056b09f6c912c5c35839cdb1b8226d3e375-2026-05-00-header.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Two months of shipping in one email — a draggable editor, cleaner MCP OAuth, stronger run controls, and plenty more. Headliners first, the full list at the end.\n\n\nApril 2026\n\n\nWorkflow Copilot v2\n\nThe workflow copilot got a lot more hands-on. Hard-cancel a run mid-flight instead of waiting"
---
_Two months of shipping in one email — a draggable editor, cleaner MCP OAuth, stronger run controls, and plenty more. Headliners first, the full list at the end._

* * *



<h2 id="april-2026">April 2026</h2>





<h3 id="workflow-copilot-v2">Workflow Copilot v2</h3>



The workflow copilot got a lot more hands-on. **Hard-cancel** a run mid-flight instead of waiting it out, and **follow it live** as it **streams status narration** and surfaces **block-level events** (starts, completions, errors) while your workflow runs. When a test trips, it hands back the **work-in-progress workflow** so you can grab the draft and keep building. Self-healed retries are now visually separated from real failures, and a quick `/discover` in chat passes the whole build straight to the copilot.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/7cde39fa290cda76a9a1e5b70e049f81e2b112687f69f9c773dfdbd64b6156ed-01-copilot-v2.png" class="kg-image" alt="Workflow Copilot v2 — live status narration with a streaming sub-event timeline, a self-healed retry shown apart from errors, and a hard-cancel button" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/1ee889414e874f522309fca5820e938719168765b38faf4eb55c4831420b5773-01-copilot-v2.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/e201a5a3b0d08a0702b16c96723bcf5fba277a0877f43164f8562ff4e2d57369-01-copilot-v2.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/7cde39fa290cda76a9a1e5b70e049f81e2b112687f69f9c773dfdbd64b6156ed-01-copilot-v2.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="mcp-oauth-across-clients">MCP OAuth, Across Clients</h3>



Connecting Skyvern over MCP is now a clean **OAuth** flow. **Multi-org users** get an organization picker right in the authorize step. Skyvern runs as a native **OpenAI Apps SDK** app, ships **safety hints** for the ChatGPT connector, documents **Codex** remote OAuth end-to-end, and adds **OpenClaw** as a new target (`skyvern setup openclaw`). Under the hood: production OAuth callbacks, plus per-tool latency and error telemetry.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/a9253bdc459777de9d4736ffa61c5f6b1e24527e2203cdf3d4afcde4b029908d-03-mcp-onboarding.png" class="kg-image" alt="MCP OAuth — an authorize dialog with an organization picker for multi-org users, alongside supported clients: Claude, ChatGPT/Apps SDK, Codex, and OpenClaw" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/93c6fb1ce2419de81839bea0a56237f558ed0de1ad8b551fb16ede88c31a73af-03-mcp-onboarding.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/0c1c69ae47cb54688a7ab7c4bc3d3a262a95138e634616675bcf5a20f3294df9-03-mcp-onboarding.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/a9253bdc459777de9d4736ffa61c5f6b1e24527e2203cdf3d4afcde4b029908d-03-mcp-onboarding.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="skyvern-schedule-skyvern-config-cli"><code>skyvern schedule</code> + <code>skyvern config</code> CLI</h3>



Your terminal is now a control panel. **`skyvern schedule`** creates, lists, toggles, and deletes workflow schedules without leaving the command line. **`skyvern config`** reads and writes org-level settings — timeouts, concurrency, default proxy — the same way. Both run over the MCP protocol, so Claude Code, Codex, and other MCP clients can use them through the same interface.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/6bc38d9d7086f9d1865a2b093caf85266c803678b50654cdd5f33d296c4ddd57-04-cli-schedule-config.png" class="kg-image" alt="Terminal showing the new skyvern schedule and skyvern config CLI commands — listing schedules in a table, creating a cron schedule, and reading an org-level config value" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/6863befe08191dc00274f979521dbf4ceb2d69c67ca9df76a76f37c433a2b023-04-cli-schedule-config.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/7249457e9132771230dd38a665bacb4b51d01dae337d88efefdb51a6139b70d1-04-cli-schedule-config.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/6bc38d9d7086f9d1865a2b093caf85266c803678b50654cdd5f33d296c4ddd57-04-cli-schedule-config.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="ai-summarize-for-workflow-outputs">AI Summarize for Workflow Outputs</h3>



Stop squinting at nested JSON. A new **Summarize with AI** button sits next to every block and workflow output — one click turns raw data into a plain-language summary of what your workflow extracted.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/e604de4c8e07fa352abb8d7d5c3263f2e40655c66bbb5595ba60ec02ef7be1de-05-ai-summarize.png" class="kg-image" alt="AI Summarize for workflow outputs — raw JSON on the left and a generated plain-language summary on the right, produced by a &quot;Summarize with AI&quot; button" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/5fd88a540ea447cbe6ae8dd4bc70c610410e1c48201823623fbaf69aa691304b-05-ai-summarize.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/233806287fcec85559893f1cdebf330b684e5b0b3aca7de20d93682b8ad7b196-05-ai-summarize.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/e604de4c8e07fa352abb8d7d5c3263f2e40655c66bbb5595ba60ec02ef7be1de-05-ai-summarize.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="workflow-level-error-code-mapping">Workflow-Level Error Code Mapping</h3>



Set your error handling once — not on every block. Workflows now take an `error_code_mapping` field that **every block inherits automatically**. Define the defaults at the top, and override per-block only where you actually need to.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/1004c5b830f13f6396c462b87aa088664ec0d2d5860c58642fc29f5500fa271c-06-error-code-mapping.png" class="kg-image" alt="Workflow-level error code mapping — error codes mapped to messages once at the workflow level, with each block inheriting them and one block adding an override" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/e47db8ae760e03d63232a075304ec2ba98c893a2b79205026cb3363b7768b0c2-06-error-code-mapping.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/505e5b20545ae9de2802a090df8351038b938b08967b2f19875a8178ae7f0dc9-06-error-code-mapping.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/1004c5b830f13f6396c462b87aa088664ec0d2d5860c58642fc29f5500fa271c-06-error-code-mapping.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="debugger-remembers-last-used-values">Debugger Remembers Last-Used Values</h3>



Less busywork between test runs. The debugger now **pre-fills the Run dialog with the values from your last run**, so you're not re-typing the same inputs every time.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/71fad5a57067866dc31453d9d530aae02a16c6c3f373e7851b92397f75dc91da-07-debugger-run-dialog.png" class="kg-image" alt="Debugger Run dialog pre-filled with parameter values from the last run, marked by a &quot;Pre-filled from last run&quot; badge" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/844d7e86d6811ba8bfc0430e4822c658d73c11c6bb40a1f314babfc423a81d9d-07-debugger-run-dialog.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/a2666ed7a9df2b5e814fa965dbfd47c86c9308613e00cffdf067ad85a239f3cb-07-debugger-run-dialog.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/71fad5a57067866dc31453d9d530aae02a16c6c3f373e7851b92397f75dc91da-07-debugger-run-dialog.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="may-2026">May 2026</h2>





<h3 id="draggable-workflow-editor">Draggable Workflow Editor</h3>



The editor is now **drag-and-drop** — reorder your whole workflow, not just tweak blocks in place. Grab any block by its handle and **drop it where it belongs**: top level, inside a branch, deep in a loop. **Collapse** blocks and entire nested containers to focus, and your layout is **remembered per workflow** across sessions. It's fully **keyboard-driven** with screen-reader announcements, and every block's config **autosaves as you type**. (Recording mode still freezes drag, so a mid-record reorder can't shift block identities under the recorder.)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/ce8530900241136ee90991ade5a1c36b2cba0bb5d523b8f2be163c7ea4976d66-2026-05-01-draggable-editor.png" class="kg-image" alt="Draggable workflow editor — a workflow canvas with a grip handle on a block mid-drag, a drop indicator between blocks, several blocks collapsed to compact headers, and a global expand/collapse control" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/e677c643dede496cccbf9e733f87393ecfd1a71775ef7cc26d926cfcd9885006-2026-05-01-draggable-editor.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/5e72532e30f20cabf2c165ab28c4f63022c0d4668dbab64c5d4865cc9e2a3e9d-2026-05-01-draggable-editor.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/ce8530900241136ee90991ade5a1c36b2cba0bb5d523b8f2be163c7ea4976d66-2026-05-01-draggable-editor.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="redesigned-run-page">Redesigned Run Page</h3>



We redesigned the run page around a compact **timeline sidebar** and a dedicated **detail panel** — scan every block at a glance, then open any one for its inputs, output, and diagnostics. A live **N-of-M completion counter** tracks progress and visibly flags blocks that never ran. The **app sidebar** got a refresh in the same pass, for cleaner navigation everywhere.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/c17b14a52f551e5df993c7df4a735b65027a0963902576673d89ddd8bf53e0ce-2026-05-02-run-page-redesign.png" class="kg-image" alt="Redesigned workflow run page — a compact timeline sidebar listing blocks with a live &quot;7/12 blocks&quot; completion counter, next to a detail panel showing the selected block's inputs, output, and diagnostics" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/7fcefd2785f136bd2ce157c14e0c14e9f199370df5d3a3c73cd528546428e653-2026-05-02-run-page-redesign.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/5b7a0245119085b3cdc94e03fc1bf63795bb2656a606df1a1b436ef4653bfd69-2026-05-02-run-page-redesign.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/c17b14a52f551e5df993c7df4a735b65027a0963902576673d89ddd8bf53e0ce-2026-05-02-run-page-redesign.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="while-loop-block">While Loop Block</h3>



Loop until you're done — not a fixed number of times. The new **While Loop block** repeats a sub-workflow **until your condition is met**: paginate until there's no Next button, retry until a status flips, drain a queue until it's empty.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/14b4c3369d4278c42980f72485e13543c0f97026eaed3db346862a5e5b84b782-2026-05-03-while-loop.png" class="kg-image" alt="While Loop block — a workflow containing a While Loop block with a condition expression, wrapping a sub-workflow that repeats until the condition turns false" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/ae473779f0cd4e9813eb0433ee2a60210472d79ea7cef12c466e0cf35d170f70-2026-05-03-while-loop.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/2218e8fa844d82971ed7dfbbd9d81124550f0c45df39fada40b815ad56c03b36-2026-05-03-while-loop.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/14b4c3369d4278c42980f72485e13543c0f97026eaed3db346862a5e5b84b782-2026-05-03-while-loop.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="analytics-dashboard">Analytics Dashboard</h3>



Your automations, by the numbers. The new **`/analytics` page** breaks down **run volume, outcomes, and trends over time** in one view — no exports, no spreadsheets. (Gated per-org, so it lights up only where it's enabled.)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8abab8017d12920e0e82531729397354e15c8229e4db86ee192da369e1bd5d28-2026-05-04-analytics-dashboard.png" class="kg-image" alt="Analytics dashboard — an /analytics page with charts of workflow run volume and success rate over time, plus per-workflow performance breakdowns" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/090961f78ef2d2bcb21e5fbd6518218918e9d064923e021afd0a8a01c2e53e85-2026-05-04-analytics-dashboard.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/4c245dc59249e193eb1df4bb9dc1ac70d8fe0d08417f89401ae4e8ecb297ffdf-2026-05-04-analytics-dashboard.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/8abab8017d12920e0e82531729397354e15c8229e4db86ee192da369e1bd5d28-2026-05-04-analytics-dashboard.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="workflow-copilot-steadier">Workflow Copilot, Steadier</h3>



The copilot is steadier when sessions get long or messy. A persistent **turn-narrative bubble** tracks what the agent is doing — and **survives page reloads**, so you never lose your place. Hit an unexpected error? It's **recoverable now**: instead of a frozen chat, you get a clear explanation and a **specific follow-up question** drawn from what it actually found missing. The **diff view** finally catches changes **nested inside conditionals and loops**, and we cleared a batch of edge cases — credential scope leaking after an edit, session context lost across turns, accepted proposals getting dropped.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/da5dc12053f5e12e54196931a028233b3d99479442ce7cf89a7c81571925ab73-2026-05-05-copilot.png" class="kg-image" alt="Workflow Copilot — a chat thread showing a persistent turn-narrative bubble summarizing the agent's current action, a recovered error with a clear explanation, and a diff panel highlighting a change nested inside a loop block" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/6abb6304ed4459bb999d03f908f806d8c817ba25bbc4388ce81846933d6e7e64-2026-05-05-copilot.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/c4b7b341a4b1eaf56d001b3539b4d0b5b452254a2372d87014f3d5a78271dd3b-2026-05-05-copilot.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/da5dc12053f5e12e54196931a028233b3d99479442ce7cf89a7c81571925ab73-2026-05-05-copilot.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="more-control-over-your-runs">More Control Over Your Runs</h3>



Manage runs in bulk — cancel, retry, cap. The new **Retry API** re-runs a workflow **from the start or from any block** — no rebuilding inputs. **`POST /runs/cancel`** cancels **many runs in a single request** and tells you exactly which succeeded and which didn't. And two new guardrails — a **runtime limit** and a **per-workflow step cap** — stop runaway executions before they burn resources.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/14f1e8a8da95480f6a032c04d21d2061fe6a01e1de6c1521e400cde81e454272-2026-05-06-run-control.png" class="kg-image" alt="Run control APIs — an API panel showing POST /runs/cancel with a list of run IDs returning cancelled and failed arrays, a retry-from-block call, and workflow settings for a max runtime and a total step cap" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/8d7152ac7fc015898e07dbaefbf118eaf643c9cb08581ac0e0d203fad9d4961e-2026-05-06-run-control.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/236874fd2e0fdce93d5bd416696e5a2816032af3d63dd8efc6bf09c9541a7ac4-2026-05-06-run-control.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/14f1e8a8da95480f6a032c04d21d2061fe6a01e1de6c1521e400cde81e454272-2026-05-06-run-control.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="self-hosting-custom-proxies-cdp-auth-headers">Self-Hosting: Custom Proxies &amp; CDP Auth Headers</h3>





<p>Route any run through <strong>your own proxy</strong> — just pass <code>{"proxy_location": {"url": "http://user:pass@host:port"}}</code> on any task, workflow, or browser-session request; no managed proxy infrastructure required. And <strong>CDP connect headers</strong> are now a first-class workflow setting — your provider's auth headers are masked, stored, and applied to the <strong>CDP connection only</strong>, never forwarded to target sites. (Plus: msedge is the new default browser for self-hosted templates, and the Docker Compose quickstart bootstraps your <code>.env</code> files on first run.)</p>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/46633fc84b0d097c722000eff5677b9a1bee5d7f09da4384b313838783a15502-2026-05-07-self-hosting.png" class="kg-image" alt="Self-hosting upgrades — a workflow settings panel with a custom proxy URL field and a masked CDP Connect Headers field, beside a terminal running the Docker Compose quickstart that auto-creates .env files" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/c874b439a78e1e5823df6e8d193fa5f6466402044047c4178dc2df60bd6c7dda-2026-05-07-self-hosting.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/052c152bcce3f7395027a2dbb6aacda2463d5252b42689b1eee2ece19e82d7e0-2026-05-07-self-hosting.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/46633fc84b0d097c722000eff5677b9a1bee5d7f09da4384b313838783a15502-2026-05-07-self-hosting.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h3 id="browser-profiles-end-to-end">Browser Profiles, End to End</h3>



Saved browser profiles — the logged-in sessions your workflows reuse — are easier to set up and manage. A **guided, step-by-step setup** walks you through creating one, a dedicated **management page** lets you **search, rename, and organize**, and saving now gives you **instant feedback** with an in-progress row the moment you hit save.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/60dd8899c5e17adcb226af35d49eff9d6c5b00c2e0ed066903e036eb19f654d5-2026-05-08-browser-profiles.png" class="kg-image" alt="Browser profiles — a dedicated browser profiles management page with search and rename controls, next to a guided step-by-step &quot;create a profile&quot; flow and an in-progress placeholder row appearing on save" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/799b4bffef763c095d57ce86620dfdc45596ab93be45187e30f6b198262de6ba-2026-05-08-browser-profiles.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/91007a06560963daba93d2e6e08104e243d889c3ee30d83010999ceba5700fc9-2026-05-08-browser-profiles.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/60dd8899c5e17adcb226af35d49eff9d6c5b00c2e0ed066903e036eb19f654d5-2026-05-08-browser-profiles.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="quick-list-%E2%80%94-april-may">Quick List — April &amp; May</h2>



**New features**

-   <strong>Credits per run &amp; run-level cost breakdown</strong> — a Credits badge on run pages, and task records now track LLM, proxy, and captcha costs as separate fields via the API
-   <strong>Credential Folders</strong> — organize credentials into named folders, with a "Move to folder" control and a folder filter that persists across credential tabs
-   <strong>Multiple schedules per workflow</strong> — independent schedules on one workflow (e.g. weekdays at 9am _and_ weekends at noon)
-   <strong>Google Sheets connector</strong> (open source) — the Sheets auth flow is now part of the OSS package
-   <strong>Tagging API</strong> — tag CRUD, tag history, tag-key management, and batch tag endpoints
-   <strong>Blank-canvas workflow entry point</strong> — a "Blank Workflow" option plus an "Or start from a description" handoff to Discover
-   <strong>MCP setup in cloud settings</strong> — a dedicated MCP configuration section in account settings
-   <strong>Action Block selector + AI fallback fields</strong> — direct control over element targeting and fallback behavior
-   <strong>Block-scoped parameter prompts in the debugger</strong> — prompts scoped to the specific block under test
-   <strong>Runs tab on the browser session page</strong> — every run tied to a given session, in one place
-   <strong>Smarter element targeting</strong> — deterministic HTML element-tree compression for more reliable selectors
-   <strong>Saudi Arabia proxy</strong> — `RESIDENTIAL_SA` added as a geolocation option
-   <strong>Password manager migration notice</strong> — UI banner when password manager settings need updating
-   <strong>Loop block failure-handling UI</strong> — clearer display of how failures are handled within loop blocks
-   <strong>OpenAI CUA model</strong> now configurable via feature flag

**Improvements**

-   <strong>Cross-run extraction cache</strong> — recurring scheduled workflows reuse extraction results across runs when page content hasn't changed, cutting redundant LLM calls
-   MCP runs are now classified as `manual`, matching UI-triggered model routing and queueing
-   MCP tool call performance telemetry tracks latency and errors across all MCP tool invocations
-   Generic remote browser vendor support — evaluate third-party CDP providers via a JSON config, including HTTP/HTTPS DevTools endpoints
-   Scheduled-trigger badge on the global Runs page
-   Faster GET workflow run API
-   Faster script-mode runs (skip speculative extract-action steps) and faster proxy selection in script mode
-   Reduced scroll-into-view settle time for snappier agent actions
-   LLM router retries with a fallback model when a response is cut off by a length limit
-   Prompt token cap prevents context-window overflows on high-complexity workflows
-   Extraction prompts capped in size before LLM calls, reducing `429 RESOURCE_EXHAUSTED` errors on high-volume workflows
-   Hardened prompt input sanitization — untrusted page content is sanitized consistently at the template layer
-   Automatic webhook retry on transient infrastructure failures
-   Signed artifact content URLs with configurable expiry, plus presigned-URL fallback for self-hosted downloads
-   Archived-artifact status indicator on runs
-   Output-parameter size cap to avoid oversized payloads
-   Double-click action support and PDF embed detection inside multi-frame pages
-   Content blocking disabled for authenticated browser launches, improving login success rates
-   Browser dialogs (alert/confirm/prompt) surfaced to the agent so it can adapt
-   TOTP scoped to the active credential, and shown as placeholder text instead of pre-filled
-   Clearer Browsers vs. Browser Profiles descriptions, and a toast action-button polish pass
-   msedge is the default browser for self-hosted templates; quickstart bootstraps `.env` files
-   Fewer screenshots captured for API-triggered runs, lowering storage overhead
-   Auto-route browser type from browser profile source
-   Reduced scrape-phase mouse movement on recaptcha-protected sites
-   Renamed browser profile reset endpoint to `/browser_session/reset_profile` for clarity
-   Removed deprecated Anthropic and Bedrock Claude model configs
