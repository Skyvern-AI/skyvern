---
title: "Skyvern 2.0: How We Reached 85.85% on the WebVoyager Eval (Updated July 2026)"
description: "Skyvern 2.0 scored 85.85% on WebVoyager using a planner-actor-validator loop. See how we built it and what the results mean. Updated July 2026."
excerpt: "Skyvern sits within the Agentic Process Automation (APA) category, where AI agents handle multi-step workflows across portals and web-based systems that have no direct API. Browser automation is one execution layer within that stack: the part that handles the credential-guarded, portal-heavy work that neither traditional RPA nor API integrations can reach.\n\nSkyvern 2.0 scored 85.85% on the WebVoyager Eval as of January 2025, placing among the top performers in web navigation tasks at that time. "
slug: "skyvern-2-0-webvoyager-benchmark-results"
publicationState: "published"
publishedAt: "2025-01-16T13:05:41.000Z"
updatedAt: "2026-07-18T02:42:10.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/ca7d4a711e57696b822c0f57b530d220bf58e2432aae1c01409e00dddb509bc5-z3obivpzxtc2l3bqpqtrp.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Skyvern 2.0: How We Reached 85.85% on WebVoyager Updated July 2026"
ogTitle: "Skyvern 2.0: How We Reached 85.85% on WebVoyager Updated July 2026"
---
Skyvern sits within the Agentic Process Automation (APA) category, where AI agents handle multi-step workflows across portals and web-based systems that have no direct API. Browser automation is one execution layer within that stack: the part that handles the credential-guarded, portal-heavy work that neither traditional RPA nor API integrations can reach.

Skyvern 2.0 **scored 85.85%** on the WebVoyager Eval as of January 2025, placing among the top performers in web navigation tasks at that time. View the full results here: <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="noreferrer">eval.skyvern.com</a>

At the time of this evaluation, that placed Skyvern 2.0 among the top performers on the WebVoyager benchmark, ahead of closed-source agents like Google Mariner. The competitive picture has since moved. Agents including Browser Use and H Company's Surfer 2 have joined the leaderboard, making this one of the more active benchmark spaces in browser automation.



<h3 id="tldr">TL;DR</h3>



-   <strong>Real World Tests:</strong> We ran all of the tests in Skyvern Cloud to get a better representation of autonomous browser operations (ie they didn’t run in any local machines)
-   <strong>Open Sourced Results:</strong> We published the entire evaluation run so you can see exactly Skyvern's thought process and actions for each case. Check it out here: <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">full evaluation run</a>
-   <strong>See it in action</strong>. Try <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud</a> or run <a href="https://github.com/Skyvern-AI/Skyvern?ref=skyvern.com" rel="dofollow">Skyvern Open Source</a> locally on your computer to see it in action!



<h2 id="agent-architecture-expansion">Agent Architecture expansion</h2>



Achieving this SOTA result required expanding Skyvern’s original architecture from a single actor prompt to a planner-actor-validator agent loop.

We will use the following example to show the differences in architecture:<br>**Go to Amazon.com and add an iPhone 16, a phone case, and a screen protector to cart**



<h3 id="skyvern-10starting-with-simple-prompts">Skyvern 1.0 - Starting with simple prompts</h3>



Skyvern 1.0 involved a single prompt operating in a loop both making decisions and taking actions on a website.

This approach was a good starting point, but scored ~45% on the WebVoyager benchmark. It was really well suited to simple single-objective goals, and could handle complex objectives if the website provided enough feedback to the client.

To show what that looks like in practice...

-   If you asked Skyvern to "Go to Amazon.com and add an iPhone 16 to cart", it would execute it flawlessly every time.
-   If you asked Skyvern to "Go to Amazon.com and add an iPhone 16, a screen protector, and a case to cart", it would add an iPhone 16 to cart, and use visual feedback to determine what to do next. You would sometimes end up with 3 iPhone 16s in the cart, or 1 iPhone 16 and 2 screen protectors.



<h3 id="improvement-1adding-a-planning-phase">Improvement #1 - Adding a planning phase</h3>



To solve this problem, we added a “Planner” phase which could decompose very complex objectives down into smaller achievable goals

1.  This allowed Skyvern to have a working memory of things it had completed and things that were still waiting to be finished
2.  This allows Skyvern to work with long complex prompts without increasing the hallucination rate

This approach worked really well! Suddenly, Skyvern was able to consistently carry out complex objectives, and we saw our WebVoyager Eval jump to ~68.7% But.. we still had some issues. Sometimes, Skyvern would carry out operations **thinking they had succeeded, but they had actually failed.** For example, If you asked Skyvern to "Go to Amazon.com and add an iPhone 16 to cart, a screen protector, and a case to cart"... in a small percentage of cases, Skyvern would attempt to click the "add to cart" button.. but the product didn't actually get added to cart!

In this case, the Task runner would report back that it was successful, and the planner would assume it worked and moved on.



<h3 id="improvement-2adding-in-a-validation-phase">Improvement #2 - Adding in a Validation phase</h3>



We added a “Validator” phase which confirmed whether or not the original goals the “Planner” generates are successfully completed or not.

1.  This acts as a supervisor function to confirm that the Task executor is achieving its objectives as expected, and report any errors / tweaks back to the Planner so it can make adjustments in real-time as needed

This was the final change needed to boost the WebVoyager accuracy to 85.85%. Now.. if a given action failed, Skyvern had the tools to identify that the desired outcome hadn't been achieved, and could generate new goals to move back to the desired state!



<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Version</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Architecture</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>WebVoyager Score</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Skyvern 1.0</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Single actor loop</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>~45%</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>+ Planner</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Planner + actor loop</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>~68.7%</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Skyvern 2.0</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Planner + actor + validator loop</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>85.85%</p></td></tr></tbody></table>
<!--kg-card-end: html-->





<h2 id="test-setup">Test Setup</h2>



All tests were run in <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud</a> with an **async cloud browser** and used a combination of GPT-4o and GPT-4o-mini as the primary decision-making LLMs. The goal of this test is to assert real-world quality: the quality represented by this benchmark is the same as what you would experience with <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern</a>'s browsers running asynchronously.

Most benchmarks are run on local browsers with a relatively safe IP, and an impressive browser fingerprint. This is not representative of how Autonomous agents will run in the cloud, and we wanted our benchmark to represent how agents would behave in production

In addition to the above, we’ve made a few minor tweaks to the dataset to bring it up to date:

1.  We've removed 8 tasks from the dataset because the results are no longer valid. For example, one of the tasks asked to go to <a href="http://apple.com/?ref=skyvern.com" rel="dofollow">apple.com</a> and check when the Apple Vision Pro will be released. By 2025 it had already been released and forgotten.
2.  Many of the flight / hotel booking tasks referenced old dates. We updated both the prompt and the answer to more modern dates for this evaluation

**For the curious**:

-   The full results can be seen here: <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="noreferrer">https:<!-- -->//eval.skyvern.com</a>
-   The full dataset is available in the <a href="https://github.com/Skyvern-AI/skyvern/tree/main/evaluation/datasets?ref=skyvern.com" rel="dofollow">evaluation dataset on GitHub</a>.
-   The full list of modifications is documented in the <a href="https://github.com/Skyvern-AI/skyvern/pull/1576/commits/60dc48f4cf3b113ff1850e5267a197c84254edf1?ref=skyvern.com" rel="dofollow">dataset modification commit on GitHub</a>.



<h2 id="test-results">Test Results</h2>



We’re doing something out of the ordinary. In addition to the results, we’re making our entire <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">benchmark run public.</a> You can go to <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">eval.skyvern.com</a> and see each entry in the WebVoyager dataset, see what actions Skyvern took and why it took them.

**Why is this important?** Most benchmarks are run behind closed doors, with impressive results being published without any accompanying material to verify the results. This makes it hard to understand how things like hallucinations or website drift over time play an effect on agent performance

We believe this isn’t aligned with our open source mission, and have decided to publish the entire eval results to the public.

-   All individual run results are published at <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">eval.skyvern.com</a>.
-   The full eval dataset is available in the <a href="https://github.com/Skyvern-AI/skyvern/tree/main/evaluation/datasets?ref=skyvern.com" rel="dofollow">evaluation dataset on GitHub</a>.



<h2 id="limitations-of-the-webvoyager-benchmark">Limitations of the WebVoyager benchmark</h2>



The WebVoyager benchmark is a broad benchmark testing a variety of prompts on 15 different websites. While it acts as a good first step in testing web agents, it only covers 15 hand-picked websites out of the millions of active websites on the internet.

We think there is real opportunity here to better compare web agents against one another with a more thorough benchmark similar to SWE-Bench.



<h3 id="a-note-on-benchmark-integrity-in-2026">A note on benchmark integrity in 2026</h3>



Since we ran this evaluation in January 2025, the leaderboard has moved. Browser Use, H Company's Surfer 2, and other agents have posted results on WebVoyager, making it one of the more active benchmark spaces in browser automation. That is a good thing. More published runs mean more public signal about where agents break. Our full run remains open at <a href="https://eval.skyvern.com/?ref=skyvern.com" rel="dofollow">eval.skyvern.com</a> so you can see exactly what Skyvern did on each task, beyond the final score.



<h2 id="what%E2%80%99s-on-the-horizon">What’s on the horizon</h2>



Browser automation is still an early-stage space with tons of room for improvement. While we've achieved a major milestone in agent performance, a few important issues are next to be solved:

1.  Can we improve Skyvern’s reasoning to operate efficiently in situations with more uncertainty? Examples include vague prompts, ambiguous or highly complex websites / tools, websites with extremely poor UX (legacy portals)
2.  Can we give Skyvern access to more tools so it can effectively log into websites, do purchases, and behave more like a human?
3.  Can we have Skyvern better memorize things it has already done in the past so it can do them again at a lower price point?



<h2 id="try-it-yourself-running-a-multi-step-task-with-skyvern-20">Try It Yourself: Running a Multi-Step Task with Skyvern 2.0</h2>



The architecture changes in this post are live in <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud</a> under the `skyvern-2.0` engine. Install the Python SDK with `pip install skyvern`, get your API key from <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud settings</a>, then call `skyvern.run_task(prompt="...", engine="skyvern-2.0", wait_for_completion=True)`. For production use, drop `wait_for_completion=True` and pass a `webhook_url` instead so your server receives the result asynchronously. Full SDK docs are at <a href="https://docs.skyvern.com" rel="dofollow">docs.skyvern.com</a>.



<pre><code class="language-python">import asyncio
from skyvern import Skyvern

skyvern = Skyvern(api_key="YOUR_API_KEY")

async def main():
    # Sync: blocks until the task finishes (good for testing)
    task = await skyvern.run_task(
        prompt="Go to Amazon.com and add an iPhone 16, a phone case, and a screen protector to cart",
        engine="skyvern-2.0",
        wait_for_completion=True,
    )
    print("Status:", task.status)
    print("Output:", task.output)

    # Async: returns immediately; result delivered to your webhook
    task_async = await skyvern.run_task(
        prompt="Go to Amazon.com and add an iPhone 16, a phone case, and a screen protector to cart",
        engine="skyvern-2.0",
        webhook_url="https://your-server.com/skyvern-webhook",
    )
    print("Task ID:", task_async.run_id)

asyncio.run(main())
</code></pre>





<h2 id="final-thoughts-on-browser-automation-and-benchmarks">Final Thoughts on Browser Automation and Benchmarks</h2>



Getting to 85.85% on WebVoyager required solving three distinct problems in sequence: a single actor that lost track of completed steps, a planner that couldn't confirm whether steps had actually worked, and a validation gap that let silent failures pass through as successes. The planner-actor-validator loop fixed all three. The table in this post shows the progression clearly: 45% with a single loop, 68.7% after adding a planner, 85.85% after closing the validation gap.

That progression is also a good map of where browser automation breaks down in production. Most failures are not random. They cluster around the same structural gaps: agents that assume success, workflows that lose state across steps, and systems that have no supervisor checking whether the desired outcome actually occurred. The architecture changes described here are a direct response to those patterns.

A few things are worth holding onto, though. WebVoyager covers 15 websites. Production workflows run across thousands of portals, many of which have aggressive anti-bot detection, credential rotation, and session timeouts that no benchmark currently measures. The 85.85% score is a real signal about agent capability on web navigation tasks. It is one data point, not a ceiling or a guarantee. What you get in production depends on the portals you are automating and how well the workflow is configured for their specific failure modes.

Within the broader Agentic Process Automation (APA) stack, browser execution is the layer that handles portals with no API, credential-guarded systems, and forms that break selector-based scripts on every layout change. Reaching this level of accuracy on a public benchmark is a meaningful indicator that the execution layer is getting production-ready. The work on orchestration, exception handling, and governance sits on top of that foundation.

If you are testing Skyvern for a real workflow, the best test is running it against your actual portals. <a href="https://meetings.hubspot.com/skyvern/demo?uuid=7c83865f-1a92-4c44-9e52-1ba0dbc04f7a" rel="nofollow">Book a demo</a> and we can work through the specific portals and authentication flows your team needs to automate.



<h2 id="faqs">FAQs</h2>





<h3 id="what-is-the-webvoyager-benchmark">What is the WebVoyager benchmark?</h3>



WebVoyager is an open benchmark that tests AI web agents across 15 different websites using real-world browsing tasks. Each task gives an agent a natural language instruction and measures whether the agent correctly carries it out on a live website. Skyvern 2.0 scored 85.85% on WebVoyager as of January 2025.



<h3 id="how-does-the-planner-actor-validator-loop-in-skyvern-20-work">How does the planner-actor-validator loop in Skyvern 2.0 work?</h3>



The planner breaks a complex goal into smaller sub-tasks and keeps a working memory of what has been completed and what still needs to happen. The actor executes each sub-task by taking actions in the browser. The validator then checks whether each sub-task actually succeeded, and if not, reports back so the planner can generate corrective goals.



<h3 id="what-was-missing-in-skyvern-10-for-multi-step-tasks">What was missing in Skyvern 1.0 for multi-step tasks?</h3>



Skyvern 1.0 used a single actor prompt running in a loop. For simple, single-objective tasks it worked well. For complex multi-step goals, it sometimes lost track of completed steps or assumed a failed action had succeeded. Adding a planning phase fixed the tracking problem; adding a validation phase fixed the silent failure problem.



<h3 id="why-does-skyvern-run-its-benchmarks-in-the-cloud-instead-of-locally">Why does Skyvern run its benchmarks in the cloud instead of locally?</h3>



Most published benchmarks run on local browsers with clean IPs and well-configured browser fingerprints. That setup does not reflect how autonomous agents actually run in production. Skyvern runs its benchmarks in Skyvern Cloud with async cloud browsers to show real-world performance, not lab conditions.



<h3 id="how-can-i-try-skyvern-20">How can I try Skyvern 2.0?</h3>



You can sign up for <a href="https://app.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern Cloud</a> or run the <a href="https://github.com/Skyvern-AI/Skyvern?ref=skyvern.com" rel="dofollow">open-source version</a> locally on your own machine.
