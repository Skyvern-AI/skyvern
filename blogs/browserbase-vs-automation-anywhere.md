---
title: "Browserbase vs Automation Anywhere: Which Browser Automation Tool is Better? (April 2026)"
description: "Compare Browserbase vs Automation Anywhere in April 2026. See which automation tool handles UI changes better and fits your team's technical needs."
excerpt: "Choosing between enterprise automation platforms like Browserbase and Automation Anywhere feels like picking which maintenance burden to live with. Browserbase removes the ops overhead of managing browser fleets but leaves you responsible for fragile selectors that break when sites redesign. Automation Anywhere centralizes bot management and cuts down on coding, but the bots still fail when UIs shift. The question that actually determines fit isn't about features or pricing. It's whether the too"
slug: "browserbase-vs-automation-anywhere"
publicationState: "published"
publishedAt: "2026-04-18T00:41:12.000Z"
updatedAt: "2026-04-18T00:41:01.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/4d057cb066988a5b57632d6b7bb2b7810e188ba307ab7d42c8d6724a01a8d848-1yikccncfasp4oxldfn4l.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Browserbase vs Automation Anywhere (April 2026)"
ogTitle: "Browserbase vs Automation Anywhere (April 2026)"
---
Choosing between <a href="https://skyvern.com/" rel="dofollow">enterprise automation platforms</a> like Browserbase and Automation Anywhere feels like picking which maintenance burden to live with. Browserbase removes the ops overhead of managing browser fleets but leaves you responsible for fragile selectors that break when sites redesign. Automation Anywhere centralizes bot management and cuts down on coding, but the bots still fail when UIs shift. The question that actually determines fit isn't about features or pricing. It's whether the tool can handle real-world websites that change without asking permission first.

**TLDR:**

-   Browserbase offers cloud browser infrastructure but leaves automation logic to you
-   Automation Anywhere provides enterprise RPA with no-code bots that break when UIs change
-   Both tools share the same core weakness: maintenance burden when websites update
-   Skyvern uses computer vision and LLMs to self-heal automations without selector updates
-   Skyvern automates browser workflows using LLMs and computer vision without brittle scripts



<h2 id="what-browserbase-does-and-how-it-works">What Browserbase Does and How It Works</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/5edb261ffc2497b97022a25e5da7e6b07e82abe1e66390cb8c10bcfb3add9961-4fw7o8ipxb5jblrlb37ru.webp" class="kg-image" alt="" loading="lazy"></figure>



To understand where each tool fits, it helps to start with exactly what Browserbase is, and what it isn't responsible for.

Browserbase is a serverless headless browser infrastructure service built for developers who need to run web automation at scale without managing their own browser fleets. It acts as the cloud layer underneath your automation logic, provisioning browsers, handling session management, and keeping things running so your team can focus on what the browsers actually do.

The service integrates with Playwright, Puppeteer, and Selenium through standard APIs and SDKs, meaning teams with existing automation code can slot Browserbase in without rewriting their scripts. It also ships with session debugging tools, live browser views, and stealth mode with CAPTCHA solving.

Pricing breaks down as follows:

-   Developer plan at $20/month covers 25 concurrent browsers and 100 browser hours
-   Team tier runs $400/month for 4,000 hours
-   Custom enterprise pricing is available beyond that

Where Browserbase draws the line is at the infrastructure boundary. It handles the browser provisioning problem well, but the automation logic (what to click, what to fill, how to handle a changing UI) is still on you. Your Playwright or Puppeteer scripts still run on top of it, which means the same selector maintenance problems follow you into the cloud. Browserbase removes the ops burden; it doesn't remove the brittleness.

**Bottom line:** Best for developer teams who need cloud browser infrastructure and already have the engineering capacity to build and maintain their own automation logic on top of it.

While browser infrastructure tools like Browserbase require custom development work to handle any real automation logic, traditional enterprise RPA faces its own set of fundamental challenges and they run deeper than just requiring more code.



<h2 id="what-automation-anywhere-does-and-how-it-works">What Automation Anywhere Does and How It Works</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/61cb9fed21a4f6f4c77882789128d52a3c54b3cf156a4529e0c0c2e1f31f88a7-3neehy7tpqvudolmgbzuk.png" class="kg-image" alt="" loading="lazy"></figure>



Where Browserbase hands the automation logic back to developers, Automation Anywhere takes a fundamentally different approach. It builds an end-to-end suite that removes the need to write code from scratch.

Automation Anywhere is one of the oldest and most widely deployed enterprise RPA platforms, founded in 2003 and built to automate business processes through software bots across both legacy and SaaS systems. Where Browserbase is infrastructure for developers, Automation Anywhere is an end-to-end RPA suite aimed at enterprise operations teams.

The platform runs on three main components: Bot Creator for designing bots with drag-and-drop tools, Bot Runner for executing those bots in production, and Control Room for scheduling, version control, and audit management. AI and machine learning are layered on top to handle exceptions and improve over time.

Pricing reflects its enterprise positioning:

-   Cloud Starter Pack starts at <a href="https://www.skyvern.com/blog/automation-anywhere-alternatives-pricing-reviews/" rel="dofollow">$750/month for SMEs</a>, including 1 unattended bot, 1 bot creator, and 1 control room
-   Attended bots cost an additional $125/month per user
-   Unattended bots cost $500/month per user
-   Enterprise contracts require direct negotiation based on bot count and modules

The platform covers a broad range of industries and focuses on no-code development, workload management, and unattended automation. But the underlying mechanic is still rule-based: bots follow predefined steps using recorded actions and selectors. When a UI changes, the bot breaks. When a workflow hits unexpected page states or conditional logic, a developer typically has to step in and fix it by hand.

**Bottom line:** Best for enterprises running structured, repeatable workflows at scale who can absorb implementation costs and dedicate resources to ongoing bot maintenance as systems change.



<h2 id="how-do-browserbase-and-automation-anywhere-compare-in-their-technical-approaches">How Do Browserbase and Automation Anywhere Compare in Their Technical Approaches?</h2>



Browserbase and Automation Anywhere are solving different problems entirely, which is worth understanding before picking one for your stack.

Browserbase sits at the infrastructure layer. It gives you a cloud-hosted browser runtime, provisioned on demand, scaled across distributed sessions, and available through standard Playwright and Puppeteer APIs. Your code runs on top of it. The browser fleet is managed; your automation logic is not.

Automation Anywhere, by contrast, is a full RPA suite. Bot Creator, Bot Runner, Control Room, the whole stack is there, designed so ops teams can build workflows without writing code. Bots record and replay interactions with digital systems: clicking buttons, entering data, moving files.

The gap that matters in production: both models are brittle when websites change. Automation Anywhere bots break because their recorded steps reference specific UI states. Browserbase-hosted Playwright scripts break for the same reason: the infrastructure is cloud-native, but the selectors underneath are not. Automation Anywhere is adding AI layers to reduce this, but the core execution model stays rule-based.

> "The effectiveness of Automation Anywhere bots is heavily reliant on regular maintenance, including updating bots to align with changes in business processes and fixing production issues."

So, the real comparison here is ops burden, not capability ceiling. Browserbase trades server management for script maintenance. Automation Anywhere trades development complexity for bot maintenance. Neither solves the underlying problem: automations that break when the target website updates its UI.

Now that we've covered what each tool does on its own, it's easier to see where the real differences lie when they're placed side by side.



<h2 id="side-by-side-comparison">Side-by-Side Comparison</h2>





<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Feature</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Browserbase</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Automation Anywhere</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Skyvern</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Technical Approach</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Serverless browser infrastructure that integrates with Playwright, Puppeteer, and Selenium</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Full RPA suite with drag-and-drop bot building using rule-based automation</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Visual reasoning with computer vision and LLMs that reads pages like a human would</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Target Users</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Developers with existing Playwright or Puppeteer investments who need cloud browser provisioning</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Enterprise operations teams running high-volume, repeatable processes with centralized bot management</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Teams hitting the maintenance ceiling on rule-based automation who need workflows that adapt to UI changes</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Starting Price</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>$20/month for 25 concurrent browsers and 100 browser hours</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>$750/month Cloud Starter Pack plus $95,000+ implementation costs for enterprise deployments</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Contact for pricing with most teams shipping first workflow in 2-3 hours</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Authentication Handling</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Left entirely to developer code with session persistence and credential handling in scripts</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Built-in Credential Vault for storing login details, but breaks when credentials expire or UI changes</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Native 2FA, TOTP, CAPTCHA solving, and credential management through Bitwarden, 1Password, and Azure Key Vault without exposing passwords to LLMs</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Resilience to UI Changes</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Scripts break when selectors reference DOM elements that change after redesigns, requiring developer updates</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Bots break when Object Cloning features are tied to specific UI states, requiring bot developers to remap elements manually</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Workflows continue running when websites redesign because visual reasoning adapts without selector updates</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Maintenance Burden</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>High: developers must update scripts every time target websites change their structure</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>High: specialists must remap cloned objects or retrain Computer Vision models after UI updates</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Low: one YAML-based workflow runs across 40 different carrier portals without remapping</p></td></tr></tbody></table>
<!--kg-card-end: html-->



Understanding these fundamental differences reveals why each approach creates distinct challenges and where those challenges hit hardest in production.



<h2 id="how-browserbase-and-automation-anywhere-handle-authentication-and-complex-workflows">How Browserbase and Automation Anywhere Handle Authentication and Complex Workflows</h2>



The architectural differences between Browserbase and Automation Anywhere become most obvious when you move beyond simple page navigation into territory that requires real-world judgment. Authentication is where that gap shows up first.

Browserbase leaves authentication entirely to the developer. Session persistence, credential handling, MFA flows: all of that lives in your Playwright or Puppeteer code. The infrastructure is managed; the logic is yours. Benchmark testing of remote browser providers found Browserbase achieving success rates of only 40-50%, with much higher browsing times, suggesting that without intelligent automation logic on top, even well-provisioned infrastructure struggles with real-world complexity.

Automation Anywhere includes a built-in Credential Vault for storing and retrieving login details. But if credentials expire or the target UI shifts, the bot fails. Its Object Cloning feature maps UI elements but is sensitive to DOM changes, meaning a website redesign can silently break a workflow that worked fine the day before. Computer Vision mode is more resilient, though it's resource-intensive and not the default path.

For multi-step workflows, the approaches diverge further:

-   Browserbase provides session recording and debugging tools, but orchestration across steps is entirely on the developer to build and maintain.
-   Automation Anywhere's Control Room handles scheduling, deployment, and monitoring centrally, giving ops teams visibility into bot health across the organization.

Both share the same core weakness: when the target website changes, something breaks. Browserbase requires a developer to update the script. Automation Anywhere requires a bot developer to remap cloned objects or retrain the Computer Vision model. The maintenance burden shifts form, but it doesn't disappear.

Beyond authentication, how easily a tool fits into your existing stack, and what skills your team needs to operate it, can be just as decisive as any feature comparison.



<h2 id="integration-complexity-and-developer-requirements">Integration Complexity and Developer Requirements</h2>



Browserbase targets developers comfortable with code. It supports JavaScript, TypeScript, and Python through the open-source Stagehand SDK, which wraps Playwright and Puppeteer behind a cleaner API. Teams already running Playwright scripts can adopt Browserbase without rewriting existing automation code, making the migration path relatively straightforward.

Automation Anywhere takes the opposite approach. Bot Creator's drag-and-drop interface lets business analysts build automations without deep coding knowledge, and pre-built templates speed up initial bot creation. That said, getting the most out of the tool still requires specialists. Businesses often need to invest in training IT staff or hire RPA developers to configure and maintain bots.

The deployment picture breaks down cleanly:

-   Browserbase requires developers to build and own all automation logic from scratch
-   Automation Anywhere provides a creation and deployment framework with lower-code options and centralized Control Room management, though specialists are still needed at scale

Neither path is low-effort once you move beyond simple use cases. But, with a clear picture of Browserbase's technical approach, it's easier to identify exactly which teams will get the most out of it and where it will fall short.



<h2 id="when-browserbase-works-and-when-it-doesnt">When Browserbase Works and When It Doesn't</h2>



Browserbase fits a specific profile well: development teams with existing Playwright or Puppeteer investments who want to eliminate infrastructure management without changing their automation code. A growth team running 100 parallel browser sessions to gather data across different accounts, for instance, gets real value from <a href="https://www.skyvern.com/blog/browserbase-vs-airtop-which-is-better/" rel="dofollow">on-demand provisioning and built-in stealth mode</a>.

Where it falls short is just as predictable. Teams without development resources, those with strict self-hosting requirements, or anyone looking for no-code automation will hit a wall quickly. Usage-based pricing also compounds at scale: long-running sessions or high-frequency workflows can push costs well beyond the base tier.

-   Works well for <a href="https://skyvern.com/blog/browserbase-vs-axiom-which-browser-automation-tool-fits-your-needs-december-2025/" rel="dofollow">developers scaling existing Playwright or Puppeteer scripts</a>
-   Struggles without in-house engineering to own automation logic
-   Not suited for no-code workflows or strict on-premise data requirements
-   Session-duration pricing becomes a real cost factor for persistent or complex workflows

**Bottom line:** Best for developer teams who need cloud browser infrastructure and already have the engineering capacity to build and maintain their own automation logic on top of it.

The same clarity applies to Automation Anywhere. It performs well in specific conditions, but those conditions are narrower than the platform's marketing often suggests.



<h2 id="when-automation-anywhere-works-and-when-it-doesnt">When Automation Anywhere Works and When It Doesn't</h2>



Automation Anywhere fits large organizations with high-volume, repeatable processes that follow consistent, predictable rules. It performs well for tasks like claims processing, data entry, and compliance reporting: workflows where the steps don't change and accuracy matters more than adaptability. The Control Room gives ops teams centralized visibility, and <a href="https://www.skyvern.com/blog/browserbase-vs-kernel-which-is-better/" rel="dofollow">attended and unattended bot modes</a> cover both human-assisted and fully automated scenarios.

Where it struggles is just as consistent: any workflow that encounters unstructured data, shifting UIs, or conditional decision-making will expose the limits of rule-based execution. When a target application updates its interface, <a href="https://www.skyvern.com/blog/browserbase-vs-uipath-which-is-better/" rel="dofollow">the bot stops working</a> until someone remaps the affected elements. That maintenance overhead compounds fast at scale.

Cost is also a real factor. A 200-person insurance company paid $180,000 per year in licensing for 15 unattended bots and 5 developer seats, plus $95,000 for a 4-month implementation through a certified partner. Those bots processed 12,000 claims monthly, replacing 8 full-time employees. The ROI existed, but the upfront investment and ongoing maintenance costs were substantial.

-   Works well for high-volume, rule-based processes that rarely change
-   Fits large enterprises with budget for implementation, training, and bot maintenance
-   Breaks down against dynamic UIs, unstructured data, or logic requiring real-time reasoning
-   Total cost of ownership is high once implementation partners and ongoing developer time are factored in

**Bottom line:** Best for enterprises running structured, repeatable workflows at scale who can absorb implementation costs and dedicate resources to ongoing bot maintenance as systems change.

Both tools share the same core failure mode: automations that break when the target website changes. Browserbase hands the maintenance problem back to developers. Automation Anywhere hands it to bot specialists. The underlying issue stays the same regardless of which layer owns it. What teams actually need is an approach that doesn't depend on predefined selectors or recorded UI states in the first place.



<h2 id="how-skyvern-solves-these-challenges-better">How Skyvern Solves These Challenges Better</h2>



Understanding where both tools break down makes it easier to see what a fundamentally different approach would need to look like.

Both Browserbase and Automation Anywhere leave teams responsible for the maintenance burden that kills automation at scale. Browserbase removes infrastructure overhead but not brittle selectors. Automation Anywhere centralizes bot management but breaks every time a target UI changes. Skyvern tackles the root cause instead of the symptoms.

The difference is how Skyvern reads web pages: visually, through computer vision and LLMs, the way a human would. There are no XPaths to break and no selectors to remap. When a carrier portal redesigns its login page, the workflow keeps running. When a payer portal adds a new form field, Skyvern reasons through it without developer intervention.

<a href="https://www.skyvern.com/blog/browserbase-vs-hyperbrowser-ai/" rel="dofollow">Authentication complexity that requires separate integrations</a> in both competing tools is built directly into Skyvern: native 2FA, TOTP, CAPTCHA solving, and credential management through Bitwarden, 1Password, and Azure Key Vault, all without exposing passwords to LLMs.

The workflow model also changes the economics. One YAML-based workflow runs across 40 different carrier portals. Automation Anywhere needs 40 bots. Browserbase needs 40 maintained scripts. Skyvern scores <a href="https://arxiv.org/abs/2401.13919" rel="dofollow">85.8%</a> on the WebVoyager benchmark, and most teams ship their first workflow in 2-3 hours instead of weeks. For teams hitting the maintenance ceiling on rule-based automation, that difference compounds fast.



<h2 id="final-thoughts-on-choosing-between-infrastructure-and-rpa-solutions">Final Thoughts on Choosing Between Infrastructure and RPA Solutions</h2>



After walking through both tools in detail, the decision comes down to one question: do you want to own the maintenance problem, or do you want a tool that eliminates it?

<a href="https://skyvern.com/" rel="dofollow">Enterprise automation</a> tools like Browserbase and Automation Anywhere handle different parts of the stack, but both leave you responsible for fixing broken workflows every time a website updates. Your team ends up in an endless cycle of selector maintenance and bot remapping instead of building new automation. Skyvern tackles the root cause with visual reasoning that adapts to UI changes automatically, so one workflow runs across dozens of different sites. <a href="https://meetings.hubspot.com/skyvern/demo?uuid=7c83865f-1a92-4c44-9e52-1ba0dbc04f7a" rel="dofollow">Book time with us</a> to see how teams ship production workflows in hours instead of weeks.



<h2 id="faq">FAQ</h2>



Still weighing your options? Here are the most common questions teams ask when comparing these tools.



<h3 id="how-do-browserbase-and-automation-anywhere-differ-in-their-technical-approaches">How do Browserbase and Automation Anywhere differ in their technical approaches?</h3>



Browserbase provides serverless browser infrastructure that integrates with Playwright, Puppeteer, and Selenium, meaning developers still write and maintain their own automation scripts on top of it. Automation Anywhere is a full RPA suite with Bot Creator, Bot Runner, and Control Room that uses drag-and-drop bot building for rule-based automation. Both require maintenance when target websites change. Browserbase needs script updates, while Automation Anywhere needs bot remapping.



<h3 id="which-tool-is-better-for-teams-without-development-resources">Which tool is better for teams without development resources?</h3>



Automation Anywhere is better suited for teams without development resources since it offers a no-code drag-and-drop interface for building bots, though specialists are still needed for implementation and ongoing maintenance. Browserbase requires developers to build all automation logic from scratch using Playwright or Puppeteer, making it unsuitable for teams without engineering capacity.



<h3 id="what-are-the-main-cost-differences-between-browserbase-and-automation-anywhere">What are the main cost differences between Browserbase and Automation Anywhere?</h3>



Browserbase uses usage-based pricing starting at $20/month for the Developer plan (100 browser hours) and $400/month for the Team tier (4,000 hours), with costs scaling based on session duration. Automation Anywhere starts at $750/month for the Cloud Starter Pack with implementation costs often exceeding $95,000 for a 4-month partner-led deployment, plus $500/month per unattended bot for production use.



<h3 id="how-do-both-tools-handle-website-changes-and-ui-updates">How do both tools handle website changes and UI updates?</h3>



Both tools break when websites change their UI structure. Browserbase-hosted Playwright scripts fail because selectors reference specific DOM elements that no longer exist after redesigns. Automation Anywhere bots break because their recorded steps and Object Cloning features are tied to specific UI states, requiring bot developers to remap elements manually after each change.
