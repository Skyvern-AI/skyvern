---
title: "Web Bench: A Fresh Approach to Comparing AI Browser Agents (Updated June 2026)"
description: "Web Bench compares AI browser agents across 5,750 tasks on 452 websites. Anthropic Claude 4 leads read tasks, Skyvern 2.0 excels at write tasks. June 2026 results."
excerpt: "Every agent vendor right now claims state-of-the-art performance on browser tasks. Skyvern, Browser-use, OpenAI's Operator are all shipping numbers that look strong on paper. But the benchmark most teams use to compare these agents, WebVoyager, covers just 643 tasks across 15 websites. That's a narrow slice of the internet, and it mostly tests agents on read-heavy tasks where the real friction hasn't kicked in yet. The harder problems are logging in, solving 2FA, filling out forms, and downloadi"
slug: "web-bench-a-new-way-to-compare-ai-browser-agents"
publicationState: "published"
publishedAt: "2025-05-29T13:47:57.000Z"
updatedAt: "2026-06-19T22:43:12.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/c7affc7d0f8f9a3f0f77003394e71df089bc5059f61aa29fff03883851b0be26-t8db2gcyfw6jw7axqchto.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Web Bench: Compare AI Browser Agents (Updated June 2026)"
ogTitle: "Web Bench: Compare AI Browser Agents (Updated June 2026)"
---
Every agent vendor right now claims state-of-the-art performance on browser tasks. Skyvern, Browser-use, OpenAI's Operator are all shipping numbers that look strong on paper. But the benchmark most teams use to compare these agents, WebVoyager, covers just 643 tasks across 15 websites. That's a narrow slice of the internet, and it mostly tests agents on read-heavy tasks where the real friction hasn't kicked in yet. The harder problems are logging in, solving 2FA, filling out forms, and downloading files, to name a few, which are underrepresented or missing entirely. And WebVoyager says nothing about the browser infrastructure running underneath each agent: whether it can get past Cloudflare, handle a CAPTCHA, or even load the target page in the first place.

So, we built something better: Web Bench. This benchmark covers 5,750 tasks across 452 websites, separates read tasks from write tasks, and measures infrastructure performance alongside agent accuracy. Here's what the data shows.

**TL;DR**

-   <a href="https://www.webbench.ai/?ref=skyvern.com" rel="noreferrer">Web Bench</a> is a new benchmark for AI browser agents: 5,750 tasks across 452 websites, with <a href="https://github.com/Halluminate/WebBench/tree/main?ref=skyvern.com" rel="dofollow">2,454 tasks open sourced</a>
-   Anthropic Claude 4 leads on read-heavy tasks (extracting information from websites)
-   Skyvern 2.0 leads on write-heavy tasks (logging in, form filling, file downloads) where all agents struggled most
-   Browser infrastructure matters as much as agent quality: proxy failures, CAPTCHAs, and auth blocks account for a meaningful share of failures
-   Full <a href="https://eval.skyvern.com/?evaluation_target=skyvern_final_eval_skyvern_2_0&amp;page=1&amp;ref=skyvern.com" rel="dofollow">results are open source</a> and viewable task by task



<h2 id="the-growing-demand-for-agent-automation">The Growing Demand for Agent Automation</h2>



Browser automation agents such as <a href="https://www.skyvern.com/?ref=skyvern.com" rel="noreferrer">Skyvern</a>, Browser-use and <a href="https://operator.chatgpt.com/?ref=skyvern.com" rel="dofollow">OpenAI's Operator</a> have taken the world by storm. These agents have been used in production for a variety of tasks, from helping people apply to jobs, downloading invoices, and even doing SS4 filings for newly formed companies. The Skyvern examples below show how browsing through a website and taking action is tackled by automated agent workflows.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/06e4c9a0c599c2e001c2962756bccec8e9f45762ad73508d1d1b13a21a8f6e9a-wbvztli45u-xan-cf8pyc.gif" class="kg-image" alt="" loading="lazy"></figure>



Skyvern attempting to purchase a product



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/64b5daad640c3a280044d5fc4bba7d771012f556a813206dda1ee6c15036a44a-4gubchwzd402xueorvyto.gif" class="kg-image" alt="" loading="lazy"></figure>



Skyvern attempting to fill out the IRS form



<h2 id="the-challenge-of-assessing-web-browser-agent-performance">The Challenge of Assessing Web Browser Agent Performance</h2>



The examples below show the challenges that agent automation has with web browsing, failing on issues like logging or closing a pop-up dialogue box.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/5bf2abb3a6bc27f058c8d9128ddb64d5d11e2b3a8cbb38cf21b2e81aca03dc85-hiyuuprju2t2mrzi-pjn.gif" class="kg-image" alt="" loading="lazy"></figure>



Can’t access <a href="http://chase.com/?ref=skyvern.com" rel="dofollow">chase.com</a>



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/2d04b831fb76a4fead59dc1251d7848062b598629f747a15369f9633401bdd8a-bqdoyxduweey-a9ggwzzn.gif" class="kg-image" alt="" loading="lazy"></figure>



Can’t close a popup dialog



<h2 id="reimagining-agent-automation-benchmarking">Reimagining Agent Automation Benchmarking</h2>



As a result, we partnered with Halluminate and created a new benchmark to better quantify these failures. Our goal was to create a new consistent measurement system for browser automation agents by expanding the foundations created by WebVoyager by:

1.  Expanding the number of websites from 15 → 452, and tasks from 642 -> 5,750 to test agent performance on a wider variety of websites
2.  Introduce the concept of READ vs WRITE tasks
    1.  READ tasks involve browsing websites and fetching data
    2.  WRITE tasks involve entering data, downloading files, logging in, solving 2FA, etc and were not well represented in the WebVoyager dataset
3.  Measure the impact of browser infrastructure (eg access the websites, solve captchas, not crash, etc)

The result was <a href="https://www.webbench.ai/?ref=skyvern.com" rel="noreferrer">Web Bench</a>, a new dataset to assess web browsing agents that consists of 5,750 tasks on 452 different websites, with <a href="https://github.com/Halluminate/WebBench/tree/main?ref=skyvern.com" rel="dofollow">2,454 tasks being open sourced.</a>



<h3 id="assessing-writeand-read-heavy-tasks">Assessing Write- and Read-Heavy Tasks</h3>



As the Web Bench graph below shows, all agents performed surprisingly poorly on write-heavy tasks (e.g., logging in, filling out forms, downloading files), which implies that this is the area for the highest opportunity for growth



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/1940559d590776f85f666204e0052f77275e6d5fd1150e9a0a78e4178d326ce7-aucuggcxnjvlrpi-qwrr.webp" class="kg-image" alt="" loading="lazy"></figure>



On the other hand, agent performance for read heavy tasks (e.g. extracting information out of websites) was better than we expected (as you can see, Skyvern had the best performance for write-heavy tasks).



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8d43f35f11d74c13decf32f4dafa7882d238c93c818c424b0c79d84af6350ef9-mmfyhzb27rkf3ht25wizj.png" class="kg-image" alt="" loading="lazy"></figure>



For read-heavy, Anthropic’s CUA had the highest performance.

You can even check out the entire <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">performance assessment</a> to see the specific agent performance of each task.



<h2 id="the-web-bench-dataset">The Web Bench Dataset</h2>



The 452 websites in the Web Bench dataset span 17 primary categories. We sampled them from the top 1,000 websites in the world by web traffic, covering a broad range of layouts, authentication patterns, and interactive elements that agents encounter in production.



<h3 id="cleaning-criteria">Cleaning Criteria</h3>



We then cleaned the dataset by removing:

-   repeat domains
-   sites without English translations
-   sites blocked by paywall

The graph below details the results of the data normalization.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/f6ae126fc5feded78c9a0dee60c5ca92511964f66af1e89f0e9634c18bc7eed4-t-vjwkwr6nrdvge2p1aqu.webp" class="kg-image" alt="" loading="lazy"></figure>





<h2 id="assessing-agents-with-web-bench">Assessing Agents With Web Bench</h2>



With the Web Bench dataset formalized and a benchmark of agent performance against write- and read-heavy tasks, we took to seeing how agents performed across the range of website categories. Our methodology constisted of:

-   We ran OpenAI Operator with a human in the loop to set a baseline for performance
-   We used consistent browser infrastructure (Skyvern’s infrastructure) when comparing the API-only models without a runtime to eliminate variables
    -   We also ran Skyvern 2.0 on Browserbase to compare the impact of infrastructure, but found (surprisingly) that Skyvern’s infrastructure was able to reliably access more websites and encountered less anti-bot issues during navigation
-   Each agent was allowed a max of 50 steps per execution
-   Each result was validated by a human in the loop to assert evaluation data quality

We gathered the results across several different metrics:

-   Accuracy (overall)
-   Accuracy (read-only tasks)
-   Accuracy (write-heavy tasks)



<h2 id="accuracy-overall">Accuracy (Overall)</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/e1d9ef60b5b30f27ac8e7dd6445c58abaf6d73d2052b5504a68cb57fac2ce5ef-vqp7j3nci-zp3jj8hfl-i.png" class="kg-image" alt="" loading="lazy"></figure>



These results were a little bit surprising, so we decided to cut the data along 2 dimensions to understand where agents may falter:

1.  Read only tasks (i.e. extracting visible data from a particular website)
2.  Write heavy tasks (i.e. logging into websites, filling out forms, downloading files)



<h2 id="accuracy-read-only-tasks">Accuracy (Read-only tasks)</h2>



Read only tasks constitute tasks that involve agents going to different websites and moving through the sitemap until a particular answer or state has been found. Unsurprisingly, these results matched the WebVoyager dataset more closely, as the WebVoyager dataset was largely curated to help agents move through websites and answer questions.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8d43f35f11d74c13decf32f4dafa7882d238c93c818c424b0c79d84af6350ef9-mmfyhzb27rkf3ht25wizj.png" class="kg-image" alt="" loading="lazy"></figure>



The biggest 2 sources of failures for read-heavy tasks are:

-   Navigation Issues (cannot figure out how to work through a page, can't solve popups)
-   Information extraction issues (doesn't pull the correct information)



<h2 id="accuracy-write-heavy-tasks">Accuracy (Write-heavy tasks)</h2>



Write-heavy tasks involve agents entering information as a user would including:

-   filling out forms
-   logging in
-   solving 2FA
-   downloading files



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/1940559d590776f85f666204e0052f77275e6d5fd1150e9a0a78e4178d326ce7-aucuggcxnjvlrpi-qwrr.webp" class="kg-image" alt="" loading="lazy"></figure>



In general, the agents had a much lower pass rate across the board. Digging a bit deeper into the failures, there were two culprits for failures that popped up:

-   Incomplete execution (hallucinating that it’s achieved the goal when it has not)
-   Unable to identify the correct element to interact with (eg can’t close a popup dialog)

In the example below, the agent is unable to close a subscription pop-up.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/0b1c0cf773650af79fe3d5d0e68d5a10c483c6f428bfb1e15db973833c48baed-kefg9yzgheyz399q3td4j.gif" class="kg-image" alt="" loading="lazy"></figure>



In the example below, the agent is unable to find and click the coupon buttons.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/dffb28a928d56cb62d356839fa37d035afc259af10ecccc7fa4b44e0de0b0006-ddhvfwphcensoxv36prky.gif" class="kg-image" alt="" loading="lazy"></figure>



These issues manifest as agents making adverse changes when filling out forms or optimistically assumes that clicking a "Submit" button completed the task when in reality a captcha appeared that now needs to be solved. This issue is very similar to the phenomenon observed in coding agents where smarter models try to "overhelp" with code changes, by either making changes to unrelated parts of the codebase, or repeatedly suggesting things that are incorrect because they're missing some important context. For a detailed breakdown of these failure modes, see our guide on common mistakes in browser automation.



<h2 id="breaking-down-agent-failures">Breaking Down Agent Failures</h2>



So why do agents fail at cetain tasks? Digging into the data, we identified several primary culprits which fell into two primary buckets:

1.  Agent Failures (eg Agents hallucinated / made poor decisions / didn’t interact with important elements)
2.  Infrastructure failures (eg Agent can’t access the website, solve a captcha to log in)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/09e40df6d6eff629152910c061085a30a70bd3e0670f69fdd0fb3c0d7dde23b2-sm142ahycyr27-ql89gfl.png" class="kg-image" alt="" loading="lazy"></figure>



The 4 biggest categories of agent errors are:

-   <strong>Navigation Issue</strong> (cannot figure out how to work through a page, can't solve popups). Dynamic overlays, cookie banners, and multi-step modal flows that appear mid-session are common culprits. The agent has no explicit instruction for handling these interruptions, so the task stalls at the obstruction instead of working past it.
-   <strong>Incomplete execution</strong> (hallucinating that it's achieved the goal when it has not). Agents see a UI state that looks like success (a loading screen, a redirect, or a thank-you message) and stop short of verifying the actual outcome. In write-heavy tasks, this often surfaces when a form submission triggers a CAPTCHA rather than a confirmation.
-   <strong>Time outs</strong> (exceeds step limits). Each run is capped at 50 steps. Agents that take redundant actions (re-reading the same page, retrying failed clicks repeatedly) burn through the step budget before reaching the goal.
-   <strong>Information extraction issue</strong> (doesn't pull the correct information). The agent reaches the right page but returns the wrong data: a cached value, a nearby field mistaken for the target, or a partial result from a table that required scrolling to complete.



<h3 id="infrastructure-issues"><strong>Infrastructure Issues</strong></h3>



The problem is that agents can only perform as well as the infrastructure on which they run. Here are the three biggest categories of infrastructure issues which lead to the failure of agents to perform those tasks:

-   <strong>Proxy</strong> (Failed to access website / website blocked). Many high-traffic websites actively block datacenter IP ranges and proxy servers. When the infrastructure can't get past this layer, the agent never sees the page. The failure happens before any reasoning takes place.
-   <strong>Captcha</strong> (Verification required to proceed and infrastructure unable to solve it). Some CAPTCHAs are built to resist automation at the browser level. If the infrastructure can't solve them, the agent is blocked before it takes a single action on the page.
-   <strong>Login/Authentication</strong> (Google Auth detecting you're a bot). Services like Google OAuth detect bot-like session patterns (new device fingerprint, no browsing history, suspicious timing). When this triggers, the login flow either fails outright or drops into a human verification loop the agent can't complete.

These findings imply that the browser infrastructure powering the agents is equally as important as the quality of the agent itself.



<h2 id="other-interesting-characteristics">Other Interesting Characteristics</h2>



While accuracy is the most important characteristic of a browser automation agent, there is also a desire to get "faster" and "cheaper" agents. Fast and cheap agents can be characterized by tracking the following metrics:

1.  Runtime duration
2.  Number of steps

While pricing models for browser agents continue to evolve, this data gives an important insight into whether pricing per hour (common amongst hosted browsers + older robotic process automation) and pricing per step (common amongst computer use APIs) is the right methodology.



<h2 id="agent-runtime-duration">Agent Runtime Duration</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/197a114258f10ad60d462a01ad67098b420e0ced082e2453fdf387bcea0dbc4a-c8v7cvgzcel62oh2cbd6.webp" class="kg-image" alt="" loading="lazy"></figure>



This metric is important for a few latency sensitive market segments / situations:

1.  Copilot-like products where a human is supervising 1 or many agents executing in parallel
2.  Phone agents referencing information / doing real-time lookups while a user is talking
3.  Websites aggregating information in real-time to show to the user (e.g. looking up flight or domain name availability)



<h2 id="number-of-steps">Number of Steps</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/f485f71a4acaa718276f9c8b4879b45d867e543116bc69989a4175cfdad76ea7-flsfp8un3j03e2tycfuag.webp" class="kg-image" alt="" loading="lazy"></figure>



Most web browsing agents’ costs scale with the number of steps (i.e. page scans) required to complete a specific task. And agents may use a varied number of steps for a few reasons:

1.  Most agents use different architectures to batch actions together to minimize the number of steps
2.  Agents eager to solve the problem may use a lot of steps in error situations to try to independently resolve their issues (e.g. chatting with support for solutions instead of terminating early)
3.  Agents using pessimistic approaches to reduce hallucinations may invalidate batches of actions whenever the website changes after a particular action (i.e. filling in a zip code field might invalidate the action plan for the rest of the page)



<h2 id="code-example-automating-a-write-heavy-task-with-skyvern">Code Example: Automating a Write-Heavy Task with Skyvern</h2>



The write-heavy tasks where agents struggled most in Web Bench (logging in, solving 2FA, and downloading files) are exactly the workflows Skyvern is built for. The example below runs a login-plus-file-download task using the Skyvern Python SDK, with 2FA handled automatically via a stored TOTP identifier.



<pre><code class="language-python">import asyncio
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")

async def download_invoice():
    task = await skyvern.run_task(
        url="https://portal.example.com",
        # Goal: log in, work through 2FA, and download the latest invoice
        prompt=(
            "Log into the portal using the stored credentials. "
            "If a 2FA code is requested, wait for it to arrive and enter it. "
            "Once logged in, go to the Invoices section, find the most recent invoice, "
            "and download it as a PDF. "
            "COMPLETE when the file has been downloaded successfully."
        ),
        # Identifier Skyvern uses to match incoming TOTP codes to this task
        totp_identifier="billing@yourcompany.com",
        # Extract the invoice metadata for downstream use
        data_extraction_schema={
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
                "invoice_date":   {"type": "string"},
                "amount_due":     {"type": "number"}
            }
        },
        # Block until the task finishes before continuing
        wait_for_completion=True,
        max_steps=30,
    )

    print("Status:", task.status)
    print("Extracted data:", task.output)
    print("Downloaded files:", task.downloaded_files)
    print("Recording:", task.recording_url)

asyncio.run(download_invoice())
</code></pre>



The same pattern extends to form submissions, multi-step checkout flows, and any portal workflow that requires authentication — all task types that showed the widest gap between agents in the Web Bench results.



<h2 id="final-thoughts-on-comparing-ai-browser-agents">Final Thoughts on Comparing AI Browser Agents</h2>



Web Bench tells a cleaner story than most benchmark comparisons do. Read tasks are largely a solved problem: agents can move through websites and pull information with reasonable reliability. Write tasks are where the real gap opens up. Logging in, filling out forms, solving 2FA, and downloading files are the operations that matter most for production agentic process automation. These are the portal-heavy, credential-guarded workflows where APIs don't reach, and every agent tested here has room to grow. The other finding that holds up on closer inspection: browser infrastructure is not a footnote. Proxy failures, CAPTCHA blocks, and login walls can account for a meaningful share of failures before the agent even gets a chance to act. At the end of the day, picking a browser automation agent means weighing two things at once: the quality of the agent's reasoning and the reliability of the infrastructure it runs on. Web Bench gives you a way to look at both, and we plan to keep expanding the dataset and the set of agents tested to make those comparisons sharper over time.



<h2 id="faq">FAQ</h2>





<h3 id="what-is-the-difference-between-read-and-write-tasks-in-web-bench">What is the difference between read and write tasks in Web Bench?</h3>



Read tasks involve going to a website and pulling information out of it: finding a product price, checking a shipping status, reading a policy. Write tasks involve changing the state of a website as a user would: logging in, filling out a form, solving 2FA, or downloading a file. WebVoyager was built almost entirely around read tasks, which is why agents looked better on that benchmark than they do in production. Web Bench separates the two so you can see exactly where agents hold up and where they fall apart.



<h3 id="why-did-agents-struggle-so-much-more-on-write-heavy-tasks">Why did agents struggle so much more on write-heavy tasks?</h3>



Two failure patterns showed up repeatedly. First, incomplete execution. Agents hallucinate that they've finished a task when they haven't, often because they see a confirmation-style UI state and stop short of verifying it. Second, element identification failures. Agents can't find or interact with a specific element like a popup close button or a coupon toggle, which blocks the rest of the workflow. Write tasks also expose infrastructure gaps more directly: a failed CAPTCHA or a login wall stops the agent entirely, whereas a read task might still return a partial result.



<h3 id="how-much-does-browser-infrastructure-affect-agent-performance">How much does browser infrastructure affect agent performance?</h3>



More than most benchmarks measure. Proxy failures, unsolved CAPTCHAs, and Google Auth detecting the agent as a bot all account for a meaningful share of total failures, and none of that is the agent's fault. An agent running on weak infrastructure will post worse numbers than the same agent on better infrastructure, even if the underlying model and reasoning are identical. That's why Web Bench tracks infrastructure failures separately from agent errors.



<h3 id="why-not-include-more-websites">Why not include more websites?</h3>



452 websites is the starting point, not the ceiling. The next iterations of Web Bench will expand the site count, add non-English websites, and broaden the category coverage. The goal for version one was to build a consistent methodology across a large enough sample to produce meaningful signal, and to do it with human validation on every result, which takes time and money to do at scale.



<h3 id="why-were-only-some-agents-benchmarked">Why were only some agents benchmarked?</h3>



Human evaluation costs can run approximately $3,000 per agent run through the full benchmark, which makes it impractical to test every agent on the market in one pass. The Halluminate team plans to release an open-source automated evaluation framework so teams can run their own agents against Web Bench without needing human review at that scale. If you want to submit results for your own agent, reach out to the <a href="https://halluminate.ai/blog/benchmark?ref=skyvern.com" rel="dofollow">Halluminate team</a>.



<h3 id="where-can-i-read-the-full-technical-writeup">Where can I read the full technical writeup?</h3>



The Halluminate team published a complete breakdown of the dataset creation process and evaluation methodology. You can read it at <a href="https://halluminate.ai/blog/benchmark?ref=skyvern.com" rel="dofollow">the Halluminate team writeup</a>.
