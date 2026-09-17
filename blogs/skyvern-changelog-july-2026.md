---
title: "Skyvern Changelog — July 2026"
description: "Everything we shipped in July 2026 — weeks of June 30, July 6, 13, 20, and 27."
excerpt: "Everything we shipped in July 2026 — weeks of June 30, July 6, 13, 20, and 27."
slug: "skyvern-changelog-july-2026"
publicationState: "published"
publishedAt: "2026-08-10T14:58:42.000Z"
updatedAt: "2026-08-10T14:58:42.000Z"
author: "suchintan"
tags: ["hash-kaitlyn"]
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/339b814466a7f861f2947115c0d9b7f1a11e366f63ac581329ceccb24b6f9731-2026-07-00-header.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
twitterLabel2: "Filed under"
twitterData2: ""
---
_Everything we shipped in July 2026 — weeks of June 30, July 6, 13, 20, and 27._

* * *



<h2 id="workflow-studio-for-everyone">Workflow Studio, for Everyone</h2>



The next-generation editor is out of preview — **Workflow Studio is now available to all users**, with a month of upgrades to match:

-   <strong>Build</strong>: a redesigned layout with resizable, drag-to-reorder panes, plus a **full-screen YAML editing mode**
-   <strong>Runs</strong>: a **Past Runs tab**, run **inputs and outputs broken down field by field**, code-block outputs shown inline, and searchable JSON
-   <strong>Polish</strong>: block search, jump-to-start/end buttons, an unsaved-changes indicator that summarizes what changed, recent-activity history on the canvas, shareable block selection in the URL, and light mode



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/67d9bf8f0bfcced40f1aa21adce3aa5d18fc1bab338bafc667089830952a365c-2026-07-01-workflow-studio-ga.png" class="kg-image" alt="Workflow Studio for everyone — the redesigned Studio editor in light mode with resizable drag-to-reorder panes, a build canvas beside a run timeline, a Past Runs tab, a block search field with jump-to-start and jump-to-end buttons, a full-screen YAML editing toggle, and an unsaved-changes indicator in the header" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/f7fea189fbae875ca9b1c912b79d22876cda22024965fddfbbd8f897f62c520e-2026-07-01-workflow-studio-ga.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/4cf80e3ffbcbe27d341b1b881448bdf547e0aa1655d9d6e1152362e7faa89634-2026-07-01-workflow-studio-ga.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/67d9bf8f0bfcced40f1aa21adce3aa5d18fc1bab338bafc667089830952a365c-2026-07-01-workflow-studio-ga.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="human-in-the-loop-approvals">Human-in-the-Loop Approvals</h2>



When a step needs a human, you're in the loop. If a workflow step requires human review, you can now **approve or reject it directly from either run view** — no side channel required. The public run status API also reports a **`paused` state**, so your integrations get an accurate picture when a run pauses, including while it waits on review.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/dd6be6336f728f1a60f2f4f7b154f28a67146b86dfbb4f1c0fa364c8084fb8f8-2026-07-02-hitl-approvals.png" class="kg-image" alt="Human-in-the-loop approvals — a workflow run view paused on a step awaiting human review, with a review card showing Approve and Reject buttons and a status chip reading paused that matches the public run status API" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/894009aa11400b41f9fc96d00de1aef13b78db2bc2e852f8d7c162dc7bdf8576-2026-07-02-hitl-approvals.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/4207d2fa07113a3f768f410cd7749c0dae604ac2f8cdf672248f57643c5f78bc-2026-07-02-hitl-approvals.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/dd6be6336f728f1a60f2f4f7b154f28a67146b86dfbb4f1c0fa364c8084fb8f8-2026-07-02-hitl-approvals.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="code-first-browser-recording-preview">Code-First Browser Recording (Preview)</h2>



Show it once, get code. **Record actions in a live browser session** and Skyvern generates **reusable workflow code** from what you did — available now as an opt-in preview. Recording itself was redesigned around a **live draft panel** instead of a separate editor, with cleaner screenshots and more step types. **Live-draft enrichment is noticeably faster** too, so suggestions show up sooner while you record.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d21445b4a901495add3df575ddb46f6953dd3f520e98f486fd86adaeef1ca8e3-2026-07-03-browser-recording-code.png" class="kg-image" alt="Code-first browser recording — a live browser session being recorded on the left with a red recording indicator, and a live draft panel on the right generating reusable workflow code step by step, each step paired with a clean screenshot" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/c2d7e08458ccdc3e1053a7ab32b830b10b435036c185e5a2493002e162766221-2026-07-03-browser-recording-code.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/a4f031b8dec593a1520031b41c244d547ea4cab878de4d41afb35eaf93cfaa4f-2026-07-03-browser-recording-code.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/d21445b4a901495add3df575ddb46f6953dd3f520e98f486fd86adaeef1ca8e3-2026-07-03-browser-recording-code.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="credentials-secrets-leveled-up">Credentials &amp; Secrets, Leveled Up</h2>



Logins got a serious upgrade this month. The Credentials page adds a **Magic Link tab**, **passkey 2FA** for password credentials on enterprise plans, and an email-based 2FA identifier — point a credential at a connected inbox and Skyvern retrieves the code automatically.

Workflows got smarter about using them, too. They can **rotate through a pool of credentials** across runs and automatically retry with a fallback credential when one fails. Runs that share a credential are serialized, so they no longer clobber each other's login sessions. The builder's credential picker is searchable with no 100-item cap. And to keep secrets out of sight, a per-workflow **mask-secrets setting** hides sensitive values in run outputs and logs, while the HTTP Request block can fetch response values as secrets and pass them between blocks.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/dd51244efbe904f75a735b57b41c87513034e1adc2f7aeea741fb3198b026756-2026-07-04-credentials.png" class="kg-image" alt="Credentials and secrets — the Credentials page with a Magic Link tab selected, a passkey 2FA badge marked enterprise, an email-inbox 2FA identifier picker, a credential rotation pool with a fallback retry arrow, a searchable credential picker, and a workflow setting toggle labeled mask secret values" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/a8e3b80f9642fbb26f161eca9a37405d10eb11590ef2ba7a31f05ec39eb86e7f-2026-07-04-credentials.jpg 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/1365ad9f2b8f01bb3eade6bc060b030d81d4e3a44a70da3bb2410c97673b7d16-2026-07-04-credentials.jpg 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/dd51244efbe904f75a735b57b41c87513034e1adc2f7aeea741fb3198b026756-2026-07-04-credentials.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="document-file-pipelines">Document &amp; File Pipelines</h2>



Files in, files out — anywhere. A new **Email Inbox block** (Gmail and Outlook) lets workflows read and act on inbox messages. A **Split PDF block** divides documents into separate files, and the File Parser now unpacks ZIP archives.

On the way out, the Download File block can deliver to **SFTP, Amazon S3, Azure, and Google Drive**, and SFTP joins the Cloud Storage upload targets. Google Drive uploads handle **files larger than 5 MB** with resumable transfers that pick up where they left off. The Cloud Storage block takes an optional prompt to upload only the files you want, and downloaded files get the correct extension detected from their content.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/ace51493fef2416f5ced7c3c9d9d10ddd50fa1f9df1a3c6f55fba3e394056d6b-2026-07-05-file-pipelines.png" class="kg-image" alt="Document and file pipelines — a workflow canvas chaining an Email Inbox block for Gmail and Outlook, a Split PDF block fanning one document into several files, and a File Parser block unpacking a ZIP archive, flowing into a download destination picker listing SFTP, Amazon S3, Azure, and Google Drive" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/db0c091f7fca7b16577e12a4c237761282d0c6a179abdd104adcc8a535a77937-2026-07-05-file-pipelines.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/c207ef96b3dc3267c2c1dc76b0b92b74d7d13382ff62d334b5c68b742f7eecb1-2026-07-05-file-pipelines.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/ace51493fef2416f5ced7c3c9d9d10ddd50fa1f9df1a3c6f55fba3e394056d6b-2026-07-05-file-pipelines.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="run-tags-a-weekly-analytics-digest">Run Tags &amp; a Weekly Analytics Digest</h2>



Organize runs your way — and get the story delivered. Runs can now be **tagged**, manually or via **auto-detected platform tags**, managed in bulk, and filtered by agent or run type in Run History. Analytics gained **grouping by run metadata** and a restructured console for a clearer read on usage and cost. And a new **weekly email digest** lands in your inbox with run volume, credit usage, a status breakdown, week-over-week trend indicators, and your lowest-success-rate workflows.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/9a06c5c191b6f251087dbf959f47c02481064faf21af1d0c89b0a3570d99428d-2026-07-06-run-tags-digest.png" class="kg-image" alt="Run tags and weekly analytics digest — a run history list with colored tags and auto-detected platform tags, a bulk tagging toolbar over selected rows, an analytics dashboard grouped by run metadata, and a weekly email digest card showing run volume, credit usage, a status breakdown, and week-over-week trend arrows" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/b6a73b30324be5db0e90f68969aa72cfbe2dcc2ae9d2215b034866f7dd919698-2026-07-06-run-tags-digest.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/848c15137d1ade20c383942655f2026497909737cb7199bea6a829ea4c298de8-2026-07-06-run-tags-digest.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/9a06c5c191b6f251087dbf959f47c02481064faf21af1d0c89b0a3570d99428d-2026-07-06-run-tags-digest.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="custom-llms-new-models">Custom LLMs &amp; New Models</h2>



Bring your own brain — or pick a new one. Organizations can now set **default Smart and Fast custom LLMs** from account settings instead of configuring them on every workflow. Custom LLM setup accepts **provider-specific parameters**, and Google Gemini is natively supported as a provider. The model menu grew too: **Claude Opus 5**, **Grok 4.5**, Gemini 3.5 Flash Lite, Gemini 3.6 Flash, and DeepSeek V4 Flash are all selectable now.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d7e4c57c97696284af1bff276390866f1b2be7958a9e9368ad552863c09326da-2026-07-07-custom-llms-models.png" class="kg-image" alt="Custom LLMs and new models — account settings with default Smart and Fast custom LLM selectors, a custom provider form showing Google Gemini with provider-specific parameters, and a model dropdown listing Claude Opus 5, Grok 4.5, Gemini 3.5 Flash Lite, Gemini 3.6 Flash, and DeepSeek V4 Flash" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/08a4c2e983388fb4223b1c5d1d919e2841ba0227c1c0a0ed89e93e376c8393bb-2026-07-07-custom-llms-models.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/25413f8637edb249858506883eaaa12ce87b435b532d39bebdb97094af4e3e1d-2026-07-07-custom-llms-models.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/d7e4c57c97696284af1bff276390866f1b2be7958a9e9368ad552863c09326da-2026-07-07-custom-llms-models.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="self-healing-you-can-see">Self-Healing You Can See</h2>



Reliability, made visible. The workflows list now shows **reliability badges** when automatic recovery has kept a workflow on track. Run pages surface a **self-heal panel** showing exactly when recovery kicked in. And **self-healing can be switched on or off** right in workflow settings.

Under the hood, runs got harder to derail:

-   Every browser action is bounded by a timeout, so a stuck action can't hang a run
-   Workflows <strong>wait through virtual queues and waiting-room pages</strong> instead of failing
-   Cloudflare Turnstile challenges are detected proactively for faster resolution, and simple arithmetic text CAPTCHAs hit during data lookups are solved automatically
-   Code-block self-healing enforces a minimum retry floor before giving up



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/2bc29127e86684b8e1865fc177b925ff50edd45881e0fa39739a78887bad3d47-2026-07-08-self-healing.png" class="kg-image" alt="Self-healing made visible — a workflows list with reliability badges, a run page panel listing automatic recovery events on a timeline, a workflow settings toggle for self-healing, and a browser view paused in a virtual waiting room with a callout showing the run patiently waiting it out" loading="lazy" width="1376" height="768" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/2aa4fa2951513401c818816548663e80a6c06cf07f3a771a8df5686c2f58a69e-2026-07-08-self-healing.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/6bce72e991df77346327bea8fd23a8bec3adf8ed118ff8138afbc1b97005387e-2026-07-08-self-healing.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/2bc29127e86684b8e1865fc177b925ff50edd45881e0fa39739a78887bad3d47-2026-07-08-self-healing.png 1376w" sizes="(min-width: 720px) 720px"></figure>



* * *



<h2 id="quick-list-%E2%80%94-july">Quick List — July</h2>



**New features**

-   <strong>In-app MCP setup</strong> — configure the MCP connector directly inside the app, plus new MCP endpoints for **1Password and Bitwarden**
-   <strong>Google integrations with org-level OAuth</strong> — centralized admin control over connected Google accounts
-   <strong>Browser session startup URL</strong> — browser sessions can start at a specific URL
-   <strong>Recurring add-on credits</strong> — set up monthly add-on credits on your subscription, with the amount configurable from billing
-   <strong>Persistent credential vault for self-hosted Docker</strong> — saved credentials now survive restarts

**Improvements**

-   MCP sessions gained a <strong>live action timeline</strong> and an artifact timeline tab
-   MCP tool results now lead with the app + recording URLs so you can jump straight in
-   MCP observe surfaces custom dropdown options that were previously hidden
-   Large workflows are easier to read and safely edit through MCP
-   Copilot follows test runs <strong>live in the Live Browser</strong>, narrates studio runs as they execute, and reveals its verification actions progressively
-   Copilot <strong>pauses to ask for a credential</strong> when one is needed and surfaces a credential card right in chat
-   Copilot auto-matches saved credentials to the login site and can read a one-time sign-in code from an email inbox
-   Redesigned Copilot composer with a clearer mode indicator
-   Copilot handles login and verification pages more safely mid-build, and says so honestly when a turn runs out of time
-   Buy additional credits on demand from the billing page — and recurring add-on credits are cheaper at <strong>$0.90 per 1,000</strong>
-   OTP parsing usage is now visible on the billing page
-   Persistent browser sessions renew their lease automatically with activity
-   Save & Reuse Session runs use a <strong>consistent egress IP</strong> and are backed by managed browser profiles
-   Browser profiles can be refreshed in place, show role badges, and are protected from deletion while in use
-   Loop block iteration limit raised from <strong>500 to 1,000</strong>
-   The Cloud Storage block now handles up to <strong>300 files</strong> per run
-   Google Sheets spreadsheet and sheet pickers accept <strong>template expressions</strong> for dynamic destinations
-   Select-option now works on <strong>custom and autocomplete-style dropdowns</strong>, not just native selects
-   Scheduling starts with a <strong>guided date/time picker</strong>
-   The editor highlights run-blocking blocks and disables Run until they're resolved
-   Improved extraction accuracy when requested data is <strong>genuinely absent</strong> from the page
-   Failed Code blocks now include a <strong>screenshot and the final page URL</strong>, and failure messages surface the actual exception and failing line
-   Run history loads much faster for workflows with many runs
-   Expired integrations re-authenticate <strong>in place</strong>, without reconnecting from scratch
-   Run views make screenshots easier to find, show output values more clearly, and display conditional/loop prompts in block settings
-   Read-only workflow canvases let you click through conditional branches
-   Login blocks prefill their goal from saved credential instructions and support an action history option
-   The Labels page adds search and protects built-in system tags
-   Files delivered via inline data URLs are captured as run files
-   ZIP parsing always returns the list of extracted files

**Bug fixes**

-   Fixed issues affecting automated solving of Cloudflare and reCAPTCHA challenges, including reCAPTCHA v2 being treated as solved before its token was ready
-   The analytics dashboard now shows credit usage from actual billing records instead of an estimate, so the numbers match your invoice
-   Fixed a race condition that could corrupt credential values — rotating or dropping characters — as they were typed into login fields
-   Fixed a regression where downloaded files could lose their original filenames, and files being uploaded under a temporary name before the browser finished renaming them
-   Fixed phone numbers being altered or rejected when country-aware form fields rewrote them with a country code
-   Fixed Cmd+V paste not working in the Live Browser view on macOS
-   Fixed screenshots, viewers, and artifact download links expiring — they now refresh automatically
-   Fixed importing workflow exports bundled as multi-workflow YAML/JSON files
-   Fixed the run detail page returning a 404 for runs based on a template workflow
-   Fixed LLM blocks not enforcing their configured output schema, which could let malformed data reach downstream blocks
-   Fixed custom segmented date inputs silently submitting the wrong month
-   Fixed a valid "no" answer from a workflow causing an otherwise successful run to be marked as failed
-   Fixed the Live Browser view showing blank frames during a run
-   Fixed missed download popups causing file-download clicks to silently fail
-   Fixed completed Copilot runs being incorrectly recorded as failures
-   Fixed failures inside a Finally block not being propagated to the run outcome
-   Fixed a Python comment causing an entire Code block to fail
