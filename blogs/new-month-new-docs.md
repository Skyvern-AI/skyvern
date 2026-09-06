---
title: "New Month, New Docs (Updated for July 2026)"
description: "We shipped real Skyvern documentation (updated for July 2026). From quickstart to self-hosting, every concept is covered with working examples and step-by-step"
excerpt: "We just shipped something we should have shipped a long time ago: real documentation.\n\nIf you've ever tried to use Skyvern and found yourself digging through GitHub issues, Discord threads, or old blog posts to figure out how something works, that's on us. We had the classic startup problem: we were shipping features faster than we could explain them.\n\nSo we built skyvern.com/docs from the ground up. Not a README with some curl examples. Actual, structured documentation that covers everything fr"
slug: "new-month-new-docs"
publicationState: "published"
publishedAt: "2026-04-01T18:16:45.000Z"
updatedAt: "2026-07-18T02:42:22.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/7a92329fbd383dfb0822c809fc5d296fe655b9231da99cf578ba783b4acfc527-ztyp8banxmbp4teqsrr3y.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "New Month, New Docs Update | Skyvern Updated for July 2026"
ogTitle: "New Month, New Docs Update | Skyvern Updated for July 2026"
---
We just shipped something we should have shipped a long time ago: **real documentation.**

If you've ever tried to use Skyvern and found yourself digging through GitHub issues, Discord threads, or old blog posts to figure out how something works, that's on us. We had the classic startup problem: we were shipping features faster than we could explain them.

So we built <a href="http://skyvern.com/docs?ref=skyvern.com" rel="dofollow">skyvern.com/docs</a> from the ground up. Not a README with some curl examples. **Actual, structured documentation** that covers everything from "what is this thing" to "how do I deploy it on my own infrastructure."

**TLDR:**

-   Good docs are the difference between a 5-minute quickstart and giving up entirely. Skyvern built skyvern.com/docs to close that gap.
-   The docs cover three distinct audiences: no-code operators, API developers (Python, TypeScript, cURL), and self-hosters running Docker or Kubernetes.
-   Workflows chain 20+ block types together; Tasks handle single-goal browser runs. Knowing which to reach for saves setup time.
-   Self-hosting is a first-class path, with LLM configuration for Vertex AI and Azure OpenAI so workflow data stays on your infrastructure.
-   Skyvern is an Agentic Process Automation (APA) platform, and the docs are organized to reflect that, from first task through production deployment.



<h2 id="the-problem-three-audiences-zero-docs"><strong>The Problem: Three Audiences, Zero Docs</strong></h2>



Skyvern has three very different types of users:

1.  <strong>No-code operators</strong> who want to automate browser tasks from a dashboard without writing a single line of code
2.  <strong>Developers</strong> who want to call our API from Python or TypeScript and integrate browser automation into their existing stack
3.  <strong>Self-hosters</strong> who want to run the whole thing on their own infrastructure with their own LLM keys

This is the class of problem Agentic Process Automation (APA) platforms are built for: browser execution is the mechanism, but autonomous multi-step operation, credential management, and structured output delivery are the actual product.

We were trying to serve all three with a README and some blog posts. It wasn't working. Users were constantly asking the same questions in Discord. Onboarding calls were turning into documentation walkthroughs. Support tickets were about things that should have been a 30-second docs lookup.



<h3 id="whats-actually-in-there"><strong>What's Actually in There</strong></h3>



The docs are organized around how you actually use Skyvern:



<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Section</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>What It Covers</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Getting Started</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Quickstart from zero to first automated browser task in ~5 minutes; API key setup, dashboard (no code required)</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Architecture</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Planner-Agent-Validator framework; visual reasoning + DOM analysis; how the agent loop handles failures and retries</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Core Concepts</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Tasks, Workflows, Blocks, Runs, Credentials, Browser Sessions, Schedules, Artifacts, Engines, each with examples</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Running Tasks</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Full API spec in Python, TypeScript, and cURL; polling, webhooks, sync waiting; artifact retrieval</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Workflows</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Visual builder and API chaining; 20+ block types (Navigation, Action, Extraction, Validation, Login, and more)</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Credentials &amp; Auth</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Encrypted credential storage, browser profile injection, 2FA detection, Bitwarden and 1Password integrations</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Self-Hosting</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Docker and Kubernetes setup; LLM config for Vertex AI, Azure OpenAI, and others; open-source deployment</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p><strong>Use Cases</strong></p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Bulk invoice downloading, job application pipelines, healthcare portal extraction, with step-by-step walkthroughs</p></td></tr></tbody></table>
<!--kg-card-end: html-->



-   <strong>Getting Started</strong>: A quickstart that gets you from zero to your first automated browser task in about 5 minutes. Sign up, grab an API key, run three lines of code (or just use the dashboard; no code required)
-   <strong>Architecture</strong>: Our Planner-Agent-Validator framework explained properly for the first time. <a href="https://www.skyvern.com/blog/how-skyvern-agents-think-and-plan-tasks/" rel="dofollow">How the agent loop works</a>, why we combine visual reasoning with DOM analysis instead of relying on brittle selectors, and what happens when things go wrong (the Validator catches it and feeds back to the Planner, which retries)
-   <strong>Core Concepts</strong>: Tasks, Workflows, Blocks, Runs, Credentials, Browser Sessions, Schedules, Artifacts, and Engines. Every concept has its own page with actual examples, not bare type definitions
-   <strong>Running Tasks</strong>: The full API spec with Python, TypeScript, and cURL examples. Polling, webhooks, and sync waiting. Artifact retrieval with screenshots, recordings, HTML snapshots, and LLM traces
-   <strong>Workflows</strong>: How to chain multiple browser tasks together with our visual builder or via API. 20+ block types including Navigation, Action, Extraction, Validation, Login, File Download, HTTP Request, Code, and more, each one documented with when and why to use it
-   <strong>Credentials &amp; Auth</strong>: <a href="https://www.skyvern.com/blog/how-skyvern-handles-authentication/" rel="dofollow">how Skyvern handles authentication</a>, plus integrations with external vaults like Bitwarden and 1Password
-   <strong>Self-Hosting</strong>: Docker and Kubernetes setup guides. LLM configuration for Vertex AI, Azure OpenAI, or whatever you're running. The whole thing is open source, and now you can actually deploy it without reverse-engineering our Docker Compose files. For context on the Kubernetes infrastructure side, the CNCF published a recent walkthrough on <a href="https://www.cncf.io/blog/2026/07/16/running-a-self-hosted-llm-in-kubernetes-with-vllm/" rel="nofollow">running a self-hosted LLM in Kubernetes</a> that covers the setup in detail
-   <strong>Use Cases</strong>: Bulk invoice downloading, job application pipelines, healthcare portal data extraction. Real workflows with step-by-step walkthroughs, not toy examples



<h2 id="code-example-running-your-first-task"><strong>Code Example: Running Your First Task</strong></h2>



The quickstart docs cover three lines of code to run your first automated browser task. Here is a working Python example using the Skyvern SDK, copy it, drop in your API key, and run it:



<pre><code class="language-python">import os
import asyncio
from skyvern import Skyvern

async def main():
    # Initialize the client with your API key
    client = Skyvern(api_key=os.getenv("SKYVERN_API_KEY"))

    # Run a task — Skyvern spins up a cloud browser, reads the page visually,
    # and returns structured output once complete
    result = await client.run_task(
        url="https://news.ycombinator.com",
        prompt="Get the title and URL of the top post",
        wait_for_completion=True,  # block until the task finishes
    )

    print(f"Status: {result.status}")
    print(f"Output: {result.output}")
    # result.recording_url gives you a full video of the browser session
    print(f"Recording: {result.recording_url}")

asyncio.run(main())</code></pre>



Set `wait_for_completion=True` and the call blocks until the task hits a terminal state, no polling loop required. For production workflows where you need to fan out multiple tasks in parallel, swap to webhooks instead and pass a `webhook_url` when calling `run_task`. The output, recording, and screenshots are all available on the returned run object regardless of which retrieval method you use.



<h2 id="why-this-matters"><strong>Why This Matters</strong></h2>



Good documentation is the difference between "I tried Skyvern and couldn't figure it out" and "I had my first automation running in 5 minutes." We were losing users at the onboarding step. Not because the product didn't work, but because they couldn't find how to make it work.

Skyvern is an Agentic Process Automation platform. Browser execution is the mechanism, but credential management, structured output, workflow chaining, and exception handling are what make it production-grade. The docs are now organized to reflect that: from first task to full deployment, each layer of the platform has its own section.

A few things we're particularly proud of:

-   <strong>Core API endpoints have working examples</strong> in Python, TypeScript, and cURL. Copy, paste, run.
-   <strong>The quickstart actually works.</strong> We tested it with people who had never seen Skyvern before. Five minutes, start to finish.
-   <strong>Self-hosting is a first-class citizen.</strong> Most cloud products bury self-hosting instructions in a footnote. We put it front and center in the Developers section because that's how a lot of our users run Skyvern.
-   <strong>The changelog is real.</strong> Every release from v1.0.15 onward, with actual descriptions of what changed and why. Adaptive caching, browser profiles, new LLM support (Gemini 3.1 Flash Lite, Claude Opus 4.6), MCP consolidation, it's all in there.



<h2 id="final-thoughts-on-skyvern-docs"><strong>Final Thoughts on Skyvern Docs</strong></h2>



Good documentation is the difference between a user who gets to their first automation in five minutes and one who closes the tab. We knew this, and we shipped the docs late anyway. That's on us.

The docs cover three distinct groups: operators who want to run automations without code, developers building on top of the API, and self-hosters who want data sovereignty and full infrastructure control. Each group has a clear path that doesn't require reading everything else first.

The bigger picture, though, is that Skyvern is an Agentic Process Automation platform. Browser execution is the mechanism. The docs now reflect that, from first task through workflow chaining, credential management, exception handling, and production deployment. A reader who works through the full docs walks away with both 'how to run a task' and 'how the whole system fits together' in a single pass.

A few sections are still thinner than we'd like (the use case walkthroughs in particular will keep growing as we add more verticals), but the foundation is there. We're shipping docs alongside features from now on, and we'd rather close gaps fast than let them become the reason someone gives up. The docs are at <a href="http://skyvern.com/docs?ref=skyvern.com" rel="dofollow">skyvern.com/docs</a>.



<h2 id="faq">FAQ</h2>





<h3 id="how-do-i-get-started-with-skyvern-if-ive-never-used-it-before">How do I get started with Skyvern if I've never used it before?</h3>



Sign up, grab an API key, and run your first automated browser task in about 5 minutes. The quickstart in the docs works with three lines of Python or entirely through the dashboard with no code required. The docs at skyvern.com/docs cover the full path from first task to production deployment, with working code examples in Python, TypeScript, and cURL.



<h3 id="whats-the-difference-between-tasks-and-workflows-in-skyvern">What's the difference between Tasks and Workflows in Skyvern?</h3>



A Task is a single goal-directed browser run: log in, extract data, submit a form. A Workflow chains multiple tasks together using 20+ block types (Navigation, Login, Extraction, Validation, File Download, and more), with the visual builder or API handling the coordination between steps. If you're automating a one-step lookup, a Task is enough; if you're building a multi-portal process like prior authorization across payer systems, Workflows are where you start.



<h3 id="can-i-run-skyvern-on-my-own-infrastructure-with-my-own-llm-keys">Can I run Skyvern on my own infrastructure with my own LLM keys?</h3>



Yes. Skyvern is open source and supports self-hosted deployment via Docker or Kubernetes, with LLM configuration for Vertex AI, Azure OpenAI, and other providers so page content and workflow data never leave your infrastructure. Self-hosted deployment is a first-class option in the docs, not a footnote, the Developers section covers it directly, including how to point the platform at customer-controlled LLM endpoints for healthcare and financial environments where data sovereignty requirements prohibit routing sensitive data through third-party AI providers.



<h3 id="what-authentication-methods-does-skyvern-support-for-portal-automation">What authentication methods does Skyvern support for portal automation?</h3>



Skyvern handles MFA, TOTP-based authenticator apps (including automatic six-digit code generation from stored secrets), email-based OTP via forwarding integration, OAuth flows, and CAPTCHA challenges through visual reasoning, all without hardcoded selectors. Phone/SMS/voice 2FA is not currently supported, which blocks portals like certain Medicaid and Medicare enrollment systems that mandate SMS-based authentication; workflows requiring those portals need an alternative authentication path confirmed before production deployment.



<h3 id="skyvern-vs-traditional-rpa-for-portal-heavy-workflows-which-fits-better">Skyvern vs. traditional RPA for portal-heavy workflows, which fits better?</h3>



For workflows running on portals that change layouts, rotate authentication flows, or span dozens of vendors, Skyvern fits better because there are no selectors to break when a portal renames a button or restructures a form, the agent re-reads the live page at runtime and keeps going. Traditional RPA tools like UiPath rely on hardcoded selectors, which means every portal update becomes a maintenance ticket; <a href="https://citrusbug.com/blog/rpa-statistics/" rel="dofollow">45% of companies experience weekly bot breakdowns</a> running selector-based automation. If your entire automation surface is a single stable internal tool with an existing API, the visual-AI layer adds overhead without adding value.
