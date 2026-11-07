---
title: "Launch Week - Day 5 - Simpler Pricing Model"
description: null
excerpt: "Automation pricing is usually optimized for the vendor, not the user.\n\nYou pay per step, per action, per minute, or per obscure unit that only makes sense if you already understand the internals of the system. The result is predictable: people under-automate, over-optimize prematurely, or stop experimenting altogether because they don’t trust the bill.\n\nWe’re changing that.\n\nToday we’re launching a new pricing model for Skyvern, designed around a single goal: make it obvious what automation cost"
slug: "launch-week-day-5-simpler-pricing-model"
publicationState: "published"
publishedAt: "2026-01-30T16:28:31.000Z"
updatedAt: "2026-01-30T16:28:31.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/9a48be837f41a6c4cac209736de5d43e1a444b3517e4d46f31064c5b21f323a3-image-7-1.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Automation pricing is usually optimized for the vendor, not the user.\n\nYou pay per step, per action, per minute, or per obscure unit that only makes sense if you already understand the internals of the system. The result is predictable: people under-automate, over-optimize prematurely, or stop experimenting altogether"
---
Automation pricing is usually optimized for the vendor, not the user.

You pay per step, per action, per minute, or per obscure unit that only makes sense if you already understand the internals of the system. The result is predictable: people under-automate, over-optimize prematurely, or stop experimenting altogether because they don’t trust the bill.

We’re changing that.

Today we’re launching a **new pricing model for Skyvern**, designed around a single goal: make it obvious what automation costs _before_ you run it, not after.



<h3 id="why-we-moved-away-from-per-step-pricing"><strong>Why we moved away from per-step pricing</strong></h3>



Previously, Skyvern was priced at **$0.05 per step**.

That model was simple for us, but it forced users to think in the wrong units. Steps are an implementation detail. Users don’t care how many steps an automation takes—they care whether a workflow is economical enough to run daily, hourly, or at scale.

Per-step pricing also discouraged iteration. Improving reliability often means adding steps, retries, or validation. Under the old model, “making automation better” and “making automation more expensive” were tightly coupled.

That’s a bad incentive.



<h3 id="credits-instead-of-steps"><strong>Credits instead of steps</strong></h3>



The new model uses **monthly credits**, with clear tiers and predictable limits. You get a fixed budget of execution capacity per month, along with concurrency, anti-bot features, authentication support, and integrations appropriate to how seriously you’re using automation.

Free lets you experiment.

Hobby lets you run real workflows.

Pro is designed for production usage.

Enterprise removes ceilings entirely.

The important part isn’t the table—it’s that you no longer need to reason about pricing at the level of individual actions. You can build workflows naturally and understand cost in aggregate, the same way you think about infrastructure or API usage.



<h3 id="pricing-that-matches-how-automation-is-actually-used"><strong>Pricing that matches how automation is actually used</strong></h3>



Real automation usage looks like this:

-   lots of experimentation up front
-   iteration to handle edge cases
-   stable workflows that run repeatedly over time

The new pricing model is designed to support that lifecycle. You can prototype freely, harden workflows without penalty, and then scale execution without constantly re-calculating cost per run.

Concurrency, CAPTCHA solving, proxies, and authentication support are now explicit parts of the plan you choose, not hidden multipliers on your bill.



<h2 id="pricing-feature-breakdown"><strong>Pricing &amp; Feature Breakdown</strong></h2>



Skyvern’s pricing is built around **execution capacity**, not implementation details. Each plan bundles together compute, concurrency, anti-bot capabilities, authentication support, and integrations so you don’t have to reason about costs at the level of individual steps.



<h3 id="how-credits-work"><strong>How credits work</strong></h3>



Credits represent a unit of browser execution. Different workflows consume different amounts of credits depending on:

-   runtime
-   page complexity
-   retries
-   anti-bot measures (CAPTCHA, proxies, geo-targeting)

The key point: **you reason about usage monthly, not per run**. You can iterate freely without worrying that adding validation or retries will unexpectedly spike costs.

* * *



<h2 id="plans-overview"><strong>Plans Overview</strong></h2>





<h3 id="free-%E2%80%94-0-month"><strong>Free — $0 / month</strong></h3>



**Best for:** experimentation, evaluation, learning Skyvern

-   ~1,000 credits / month (≈170 actions)
-   1 concurrent run
-   Datacenter proxies
-   Country-level geo-targeting
-   Webhooks
-   Community support

This tier exists so you can:

-   build real workflows
-   understand how Skyvern works
-   validate feasibility before committing

It is intentionally constrained on concurrency and anti-bot features.

* * *



<h3 id="hobby-%E2%80%94-29-month"><strong>Hobby — $29 / month</strong></h3>



**Best for:** side projects, early production use, individual developers

-   ~30,000 credits / month (≈1,200 actions)
-   10 concurrent runs
-   Basic CAPTCHA solver
-   Stored credentials
-   Datacenter proxies
-   Country-level geo-targeting
-   Webhooks
-   Email support

This is the first tier where automation becomes _useful at scale_. You can run workflows repeatedly and in parallel, but without advanced anti-bot or enterprise authentication requirements.

* * *



<h3 id="pro-%E2%80%94-149-month"><strong>Pro — $149 / month</strong></h3>



**Best for:** production workflows, internal tools, small teams

-   ~150,000 credits / month (≈6,200 actions)
-   25 concurrent runs
-   Advanced CAPTCHA solver
-   Residential proxies
-   Country / state / city-level geo-targeting
-   2FA / TOTP support
-   1Password integration
-   Team workspaces
-   Webhooks
-   Priority support

This is the tier optimized for **real production automation**:

-   authenticated workflows
-   fragile or adversarial sites
-   shared usage across a team

Most customers running Skyvern in a business context land here.

* * *



<h3 id="enterprise-%E2%80%94-custom"><strong>Enterprise — Custom</strong></h3>



**Best for:** regulated environments, high volume, zero ceilings

-   Unlimited credits
-   Unlimited concurrent runs
-   Advanced CAPTCHA solver
-   Residential proxies
-   Fine-grained geo-targeting
-   SOC 2 report
-   HIPAA compliance
-   Dedicated Slack support
-   Azure Key Vault integration
-   Bitwarden integration
-   Custom code blocks
-   Human-in-the-loop workflows

Enterprise removes both **technical ceilings and organizational friction**. It’s designed for companies where automation is business-critical and must integrate cleanly with existing security, compliance, and credential infrastructure.

* * *



<h2 id="feature-categories-explained"><strong>Feature Categories Explained</strong></h2>





<h3 id="execution-scale"><strong>Execution &amp; Scale</strong></h3>



Concurrency and queued runs determine how many workflows can execute simultaneously. This matters once automations move from “nice to have” to “operational dependency.”



<h3 id="anti-bot-geo-targetting"><strong>Anti-Bot &amp; Geo targetting</strong></h3>



CAPTCHA solving, proxy type, and geo-targeting directly affect success rates on modern websites. These are bundled at the plan level so you don’t have to toggle cost multipliers per run.



<h3 id="authentication-secrets"><strong>Authentication &amp; Secrets</strong></h3>



Support for 2FA, password managers, and secret vaults becomes essential for real-world workflows. These features are isolated to higher tiers to match the environments that need them.



<h3 id="integrations"><strong>Integrations</strong></h3>



API access and webhooks are available across all plans. Team workspaces, custom code blocks, and human-in-the-loop flows are introduced as automation becomes collaborative and business-critical.



<h3 id="compliance-support"><strong>Compliance &amp; Support</strong></h3>



Compliance artifacts and dedicated support are gated to Enterprise, where procurement, audits, and SLAs matter.
