---
title: "Can AI Build Web Scrapers Easily? The Reality Behind Automated Code Generation (Updated June 2026)"
description: "Skyvern now writes and maintains its own code, making AI scraper building 2.7x cheaper and 2.3x faster. Learn how explore-replay works in June 2026 (Updated)."
excerpt: "You can get AI to write a working web scraper in minutes. But the first time a form changes, a portal goes offline, or a dropdown starts behaving like a textbox, that scraper breaks and nobody told it why. That's the real challenge behind automated code generation. It turns out the fix isn't smarter code generation, it's a better split between when an agent reasons and when it just runs.\n\nThe shift shows up in how buyers frame the problem. Two years ago, the ask was \"Can AI automate this workflo"
slug: "asking-ai-to-build-scrapers-should-be-easy-right"
publicationState: "published"
publishedAt: "2025-10-17T17:46:58.000Z"
updatedAt: "2026-06-29T16:06:48.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/5584a2059da87ee06cceb1ad9cbaf0a2d51d0fbbe59e88304ecd9cd071997af7-hini2hbridkirevsj-vat.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "AI Scrapers: Should It Be Easy? (Updated June 2026)"
ogTitle: "AI Scrapers: Should It Be Easy? (Updated June 2026)"
---
You can get AI to write a working web scraper in minutes. But the first time a form changes, a portal goes offline, or a dropdown starts behaving like a textbox, that scraper breaks and nobody told it why. That's the real challenge behind <a href="https://www.skyvern.com/blog/what-is-ai-automation-complete-guide/" rel="dofollow">automated code generation</a>. It turns out the fix isn't smarter code generation, it's a better split between when an agent reasons and when it just runs.

The shift shows up in how buyers frame the problem. Two years ago, the ask was "Can AI automate this workflow?" Now it's "Can your agent _maintain_ the automation after it breaks?" Governance comes up in the first call. Audit trails, approval gates, and exception escalation aren't nice-to-haves; they're table stakes. The day-to-day question and the compliance question came together, and that convergence is what makes the learn-replay architecture worth building now. Those requirements are how buyers describe Agentic Process Automation (APA) without using the term. The learn-replay architecture below is what APA looks like at the execution layer.

Getting AI to write a web scraper isn't the hard part anymore. What's hard is making that scraper survive contact with the actual internet: form fields that change, portals that go down on weekends, and layouts that look completely different two weeks later. That gap between code that runs and code that recovers is exactly what <a href="https://www.skyvern.com/blog/what-is-ai-automation-complete-guide/" rel="dofollow">automated code generation</a> still struggles to close on its own.

**TLDR:**

-   AI can write a web scraper, but getting it to recover from portal downtime and layout changes is the hard part.
-   The learn-replay architecture splits automation into two modes: an agent learns the flow once, then deterministic Playwright code runs it repeatedly.
-   Replay mode cuts run time from 279 seconds to 120 seconds (2.3x faster) and cost from $0.11 to $0.04 per run (2.7x cheaper).
-   Intent metadata captures the "why" behind each action, letting the system recover when selectors break instead of failing silently.
-   Skyvern uses this learn-replay pattern across invoice downloading, government form filing, purchasing, and legacy data extraction workflows.

It's <a href="https://github.com/Skyvern-AI/Skyvern?ref=skyvern.com" rel="dofollow">an open source tool</a> that helps companies automate things in the browser with AI. We use computer vision + LLMs to turn prompts into automations that run. We serve both technical and non-technical customers, and have helped them automate things like applying to jobs, fetching invoices or utility bills, filling out government forms, and purchase products from hundreds of different websites. At the platform level, Skyvern is an Agentic Process Automation (APA) platform: browser execution is the mechanism, but multi-step planning, exception handling, and structured output delivery are the actual product.



<h2 id="why-this-matters-now">Why This Matters Now</h2>



Some of you may remember our <a href="https://www.skyvern.com/blog/what-is-ai-automation-complete-guide/" rel="dofollow">full guide to AI automation</a> from earlier this year. All of the discussion circled around the same idea: "Building the automation is the hard part… we just want Skyvern to write the code".

We agreed. Keeping the agent in the loop means invoking an expensive and non-deterministic LLM call on every run. If Skyvern could compile its reasoning into code and run that instead of keeping an LLM in the loop, automations would become **faster, cheaper, and more reliable.**

So we tried to teach Skyvern to do exactly that… but it turns out, asking AI to write code the same way you and I would wasn’t easy. We ran into two big problems:

1.  Requirements for automations are ambiguous at best, and misleading at worst, and even humans struggle to define them clearly
2.  The internet is messy: drop-downs masquerade as textboxes, checkboxes that are always checked, and search bars that are secretly buttons.

Getting an agent to work through that chaos, understand intent, and still produce maintainable code came through one major breakthrough: **reasoning models**.

Reasoning models unlock two important capabilities:

1.  They <a href="https://www.skyvern.com/blog/web-bench-a-new-way-to-compare-ai-browser-agents/" rel="dofollow">boosted the agent accuracy</a> enough for production use
2.  They let the agent use its recorded path to write a script resembling something an engineer would write

This is too abstract. When does this matter?

Before we get into the solution, let's look at a real-world example: Registering new companies for payroll with <a href="https://sa.www4.irs.gov/modiein/individual/index.jsp?ref=skyvern.com" rel="dofollow">Delaware.gov</a>



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/12b6bd8ecc4df49cd89400002abf652ca0c12699be4f26ea3cbfd556b872e999-cugdfyv2klplaw63sxl-j.webp" class="kg-image" alt="" loading="lazy"></figure>



Here’s a simple prompt that reliably powers the workflow:

> Your goal is to fill out the EIN registration form. Fill out the form until you're at the form confirmation page with a summary of all information. Your goal is complete once you see a summary of all of the information.

ein\_info: {{ein\_info}}

Writing deterministic code should be easy right?

Here’s what a naive AI Generated implementation looks like:



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d45a8d0df0e87eb50c4126e95cd5fb17d16d981d0f4d4421a4446954e9c6f7e4-sdmrw8gkzmrfpgl9hjvp0.png" class="kg-image" alt="" loading="lazy"></figure>



And here’s where it falls apart almost immediately.

1.  Coupled interactions. Choices on this form aren’t independent. Sometimes radio buttons are linked together, but aren’t represented as such in the DOM. Other times, different buttons trigger different follow-up questions, so a static script breaks as soon as you pick something unexpected.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/073cdeaad5244616cb8209f507f84c4bd0dfaa62ffd945717510d9e926b7dc2f-soreq1rd3yazsvy6lagx6.png" class="kg-image" alt="" loading="lazy"></figure>



**Random failures.** Government websites love to go down at night, change field layouts between sessions, or throw you a “try again later” page mid-run.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/a24147b8ad6ff49de55b085e258b5087a57c77113a81147a9b27014484dce464-3zpxr68briwdn0exidyyp.png" class="kg-image" alt="" loading="lazy"></figure>





<h3 id="coupled-interactions"><strong>Coupled interactions</strong></h3>



Consider the radio button example above. Any seasoned developer would know that these legal structures are linked together. You’d instinctively model them as a finite set of entity types, and create a tree-like script that branches into different paths based on the input.

But that abstraction doesn't exist for an agent. To agents like Skyvern, this is just a list of buttons. The relationship between the users' input and the available set of legal structures doesn't exist ahead of time; it must be determined at runtime.

The agent has to infer, from the DOM and the page transitions, which choices lead where.



<h3 id="random-non-deterministic-failures">Random non-deterministic failures</h3>



Agents shine when things don't go as planned. We don't want to hard code every single edge case when compiling an agent into a deterministic script.. because we're back to writing brittle scripts. Instead, we want to call on agents for these situations.

Take this government form: Delaware’s portal is unavailable at night or over the weekend. Or sometimes, it’ll require you to call the IRS or sending them a fax / mail to proceed with the form. You want some intelligence in the loop to handle these scenarios gracefully.

So.. how can you codify this in an agent?

After a few runs like the ones above, we realized “have the agent write code” wasn’t enough. We needed to copy how developers actually work: figure out the flow, add logic where it breaks, and bake that behavior into Skyvern.

So we split its job into two:

1.  Learn mode, where the agent figures out how to work through a website for a given flow, generating any metadata necessary for it to operate in subsequent runs
2.  Replay mode, it compiles those learnings into deterministic Playwright and runs fast and cheap, only falling back to the agent when something new or weird happens



<h3 id="learn-once-capture-the-agents-path">Learn once: capture the agent's path</h3>



Let's start with a plain prompt for Skyvern. The `ein_info` field is just a json blob with all of a company's metadata (entity type, responsible party, etc). The goal of this learn-mode run isn't to finish fast, the run's goal is to learn the flow and record a path we can compile later.

> Go to <a href="https://sa.www4.irs.gov/modiein/individual/index.jsp?ref=skyvern.com" rel="dofollow">https:<!-- -->//sa.www4.irs.gov/modiein/individual/index.jsp</a><br>and generate an SS4 with the following information: {{ ein\_info }}



<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Execution Mode</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>How It Works</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Average Run Time</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Average Cost Per Run</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Agent-Based (Learn Mode)</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>LLM analyzes each page at runtime, decides actions, and records the action path for later compilation</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>278.95 seconds</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>$0.11</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Code-Based (Replay Mode)</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Generated Playwright script runs deterministically, falling back to agent only when page structure changes</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>119.92 seconds (2.3x faster)</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>$0.04 (2.7x cheaper)</p></td></tr></tbody></table>
<!--kg-card-end: html-->





<h3 id="step-1-generate-a-naive-script">Step 1: generate a naive script</h3>



From that recorded path, the agent can spit out a basic Playwright script. It runs, but it's brittle: no context, no fallbacks:



<pre><code class="language-python"># Naive AI-generated Playwright script: runs once, breaks easily
from playwright.sync_api import sync_playwright

def fill_ein_form(ein_info: dict):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://sa.www4.irs.gov/modiein/individual/index.jsp")

        # Hardcoded selector: breaks if the IRS renames this element
        page.click("#entity-type-corporation")

        # Hardcoded next step: breaks if a different entity type is passed
        page.click("#continue-btn")

        # No fallback if the portal is down or the form layout changes
        page.fill("#responsible-party-name",
                  ein_info["responsible_party"]["name"])
        page.fill("#responsible-party-ssn",
                  ein_info["responsible_party"]["ssn"])

        page.click("#submit")
        browser.close()</code></pre>



**What’s missing:** it doesn’t know _why_ it’s clicking “Corporation,” what should appear next, or how to recover if the DOM shifts (or the portal is down). That’s the gap we close in the next section (intent metadata + deterministic replay with targeted fallbacks).



<h3 id="step-2-ask-the-agent-to-write-down-its-intention-so-we-can-re-use-it-later">Step 2: Ask the agent to write down its intention so we can re-use it later</h3>



Exploration gives us a working script, but it’s brittle because it only knows _what_ to click, not _why_. The fix was to capture **intents** to every action so the run can recover when the page shifts.

To get to this intention, we generate 2 additional parameters at runtime: `user_detail_query` and `user_detail_answer` to capture the **essence** of the action (beyond the interaction itself)

Then, we pass it through another LLM call to reverse engineer the action into the following:



<pre><code class="language-python"># What Skyvern records for each action during learn mode
action_with_intent = {
    "action_type": "CLICK",
    "selector": "#entity-type-corporation",

    # Intent metadata: the "why" behind the click
    "user_detail_query": "What legal structure is the entity registering as?",
    "user_detail_answer": "Corporation (standard C-Corporation).",

    # Used at replay time if the selector above no longer resolves
    "fallback_intent": (
        "Find and select the option that represents 'Corporation' "
        "as the legal entity type."
    ),
}</code></pre>



If this fails at replay time, we don't blindly guess a new selector. We reuse the **intention** to recover:

1.  Try an alternate selector for the same intention (looser match, nearby label, aria text)
2.  If the flow changed, ask the model <strong>once</strong>: “How do I ‘Select legal structure: Corporation’ on this page?”
3.  If we hit a dead end (downtime/error), fallback to the original prompt to decide what to do next

At this point, we have generated code that looks like this:



<pre><code class="language-python"># Intent-enriched script generated after learn mode
from playwright.sync_api import Page
from skyvern import Skyvern

async def select_entity_type(
    page: Page,
    skyvern_client: Skyvern,
    browser_session_id: str,
):
    """
    Intent: Select legal structure as Corporation
    user_detail_query: What is the legal structure of the entity?
    user_detail_answer: Corporation (C-Corp)
    """
    try:
        # Replay path: deterministic Playwright selector runs first
        page.click("#entity-type-corporation")

    except Exception:
        # Selector broke: fall back to intent-based recovery.
        # The agent gets one targeted call to accomplish the same goal.
        await skyvern_client.run_task(
            prompt=(
                "Select the legal structure for Corporation. "
                "Look for a radio button or link labeled 'Corporation' or 'C-Corp'."
            ),
            browser_session_id=browser_session_id,
        )</code></pre>





<h3 id="step-3run-it-on-the-cheap">Step 3 - Run it on the cheap</h3>



Now that we have a plan and a fallback in place, subsequent runs are all using plain playwright, with no LLM in the loop.

Here's how that split looks in the Skyvern Python SDK, one call with `run_with="agent"` to learn the flow, then `run_with="code"` for every subsequent run:



<pre><code class="language-python">from skyvern import Skyvern
import asyncio

skyvern = Skyvern(api_key="YOUR_API_KEY")

ein_info = {
    "entity_type": "Corporation",
    "responsible_party": {"name": "Jane Smith", "ssn": "XXX-XX-XXXX"},
    "business_name": "Acme Corp",
    "state_of_formation": "Delaware",
}

async def run_ein_workflow():
    # Learn mode: LLM works through the form once, records intent metadata
    learn_run = await skyvern.run_task(
        url="https://sa.www4.irs.gov/modiein/individual/index.jsp",
        prompt=(
            f"Generate an SS4 EIN using: {ein_info}. "
            "COMPLETE when you see the confirmation summary page."
        ),
        run_with="agent",  # LLM in the loop, builds path and intent map
        webhook_url="https://your-system.example.com/webhooks/ein",
        wait_for_completion=True,
    )
    print(f"Learn run: {learn_run.run_id}, status: {learn_run.status}")

    # Replay mode: compiled Playwright handles all subsequent runs
    # Result: ~2.3x faster and ~2.7x cheaper than agent mode
    replay_run = await skyvern.run_task(
        url="https://sa.www4.irs.gov/modiein/individual/index.jsp",
        prompt=(
            f"Generate an SS4 EIN using: {ein_info}. "
            "COMPLETE when you see the confirmation summary page."
        ),
        run_with="code",  # deterministic Playwright, LLM only on fallback
        webhook_url="https://your-system.example.com/webhooks/ein",
        wait_for_completion=True,
    )
    print(f"Replay run: {replay_run.run_id}, status: {replay_run.status}")

asyncio.run(run_ein_workflow())</code></pre>



Internal benchmarking across production customer workflows showed:

-   Average automation run time goes from 278.95s → 119.92s (2.3x faster)
-   Average run cost goes from $0.11 → $0.04 (2.7x cheaper)
-   And maybe more important than either: <strong>runs are now deterministic.</strong>



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/747334fe181d958ba5f8528af935280924985abe5e1b7554c740f77a33d41673-v4-lhix-kow64qh1pfwkq.png" class="kg-image" alt="" loading="lazy"></figure>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/593be4a343ba009ff735ac2733b60ef5a9581ce1ab291c3e0dcc17975bf75fba-3awt343af-xwj-k9c-5nk.png" class="kg-image" alt="" loading="lazy"></figure>



How is this being used in the wild?

Skyvern's learn-replay pattern is already running quietly inside production workflows across invoice retrieval, purchasing, and government forms that, for many automation teams, used to require brittle, human-maintained scripts.



<h3 id="invoice-downloading"><strong>Invoice Downloading</strong></h3>



Agents log into vendor or utility portals with 1000s of different accounts, move to the right billing period, and pull invoices. When layouts or date filters change, the intent metadata lets them recover automatically instead of failing.



<h3 id="purchasing"><strong>Purchasing</strong></h3>



Teams use it to automate repeat purchases: think renewing software licenses or buying supplies through the same vendor dashboard each month. The first run learns the checkout path, the fallbacks handle the variety of products, and the replays run deterministically, flagging if a price or SKU changes.



<h3 id="data-extraction-from-legacy-systems"><strong>Data Extraction from Legacy Systems</strong></h3>



Skyvern moves through authenticated dashboards, scrapes tables or PDFs, and pushes the structured output into a database via Webhooks. If the DOM shifts, Skyvern reuses the same intention ("extract transaction rows") to remap selectors.



<h3 id="government-form-filling"><strong>Government Form Filling</strong></h3>



From payroll registration to business license renewals, Skyvern handles long, multi-step government forms that occasionally break static scripts. Learn mode figures out the flow once, then replay mode repeats it safely.



<h2 id="what%E2%80%99s-next-is-it-perfect-today">What’s next? Is it perfect today?</h2>



Not quite. The architecture works well, but there are still a few places where we can make it smarter and cheaper:



<h3 id="analyze-groups-of-runs-when-generating-code"><strong>Analyze groups of runs when generating code</strong>.</h3>



Right now, we don’t aggregate insights across failures. Each replay fixes itself in isolation. If we could analyze _many_ runs together, spotting which selectors break, which flows diverge, we could automatically generalize better code and reduce the need for fallbacks. That’s especially useful for workflows that branch like trees (different inputs → different paths).



<h3 id="cache-data-extractions"><strong>Cache data extractions</strong>.</h3>



During data extraction, we still rely on the LLM to “read” the page each time because many of our users want to both extract and summarize information at the same time. For example, if you ask Skyvern to pull the summaries of the top five posts on Hacker News, it currently parses the DOM from scratch. We’d like to trace _how_ the model found those elements (which selectors, which substrings) and reuse that mapping. That alone could make scraping and data-harvesting flows an order of magnitude cheaper.



<h3 id="expose-everything-through-the-sdk"><strong>Expose everything through the SDK.</strong></h3>



We think it will be valuable for developers using the Skyvern SDK to auto-generate these scripts for any ai actions / workflows they run, and use them automatically for subsequent runs. It currently requires a Skyvern server running, but soon it will be the default behaviour.



<h2 id="final-thoughts-on-building-web-scrapers-with-ai">Final Thoughts on Building Web Scrapers with AI</h2>



The learn-replay architecture is a good example of what Agentic Process Automation looks like in practice: an agent that reasons through a workflow once, compiles that reasoning into deterministic code, and falls back to its own judgment when something unexpected shows up. Run it via our <a href="https://github.com/Skyvern-AI/Skyvern?ref=skyvern.com" rel="dofollow">Skyvern Open Source</a> or <a href="http://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud</a> versions and let us know what you think!



<h2 id="faq">FAQ</h2>





<h3 id="what-is-the-learn-replay-architecture-and-how-does-it-differ-from-running-an-ai-agent-every-time">What is the learn-replay architecture and how does it differ from running an AI agent every time?</h3>



Learn mode runs once with an LLM analyzing each page, recording both the actions taken and the intent behind them: which button to click and the reason behind each choice. Replay mode compiles that recording into deterministic Playwright code that runs without an LLM in the loop, falling back to the agent only when page structure changes or unexpected errors appear. The result is automation that behaves like a maintained script but recovers like an agent.



<h3 id="what-kind-of-performance-gains-can-i-expect-from-switching-to-replay-mode">What kind of performance gains can I expect from switching to replay mode?</h3>



Across production workflows, replay mode averages 2.3x faster execution (119 seconds vs. 279 seconds) and 2.7x lower cost ($0.04 vs. $0.11 per run) compared to keeping the LLM in the loop on every step. Exact numbers vary by workflow complexity and how frequently the target site changes its layout.



<h3 id="should-i-use-learn-replay-or-keep-the-agent-running-on-every-task">Should I use learn-replay or keep the agent running on every task?</h3>



Learn-replay is the right call for workflows you run repeatedly: invoice downloads, government form submissions, recurring purchases, where the flow is largely stable but occasional layout changes still need intelligent recovery. For one-off tasks or workflows that change so frequently the compiled code would need constant regeneration, keeping the agent in the loop the whole way through makes more practical sense.



<h3 id="what-happens-when-a-government-portal-goes-down-or-changes-its-layout-mid-run">What happens when a government portal goes down or changes its layout mid-run?</h3>



The replay script tries alternate selectors first, then asks the model how to accomplish the original intent on the current page state. If the portal is unavailable or returns an error page, Skyvern uses the original prompt to decide whether to retry, wait, or flag the run for human review, so a portal outage results in a clear escalation path instead of a silent failure or a broken script that someone has to patch manually.



<h3 id="how-does-intent-metadata-help-when-selectors-break">How does intent metadata help when selectors break?</h3>



Every action in the learn-mode run gets two additional parameters: a query capturing what information the agent needed and an answer capturing the essence of the action. The system knows the "why" as well as the "what." When a selector fails at replay time, the agent reuses that intent to try a looser selector match, check nearby labels, or ask the model once how to accomplish the same goal on the updated page, instead of failing silently or guessing blindly.
