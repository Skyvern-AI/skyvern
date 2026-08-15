---
title: "Launch Week - Day 4 - SOP Upload to build new Workflows"
description: null
excerpt: "Most companies already know how their work gets done.\n\nIt’s written down somewhere:\n\n * internal SOPs\n * runbooks\n * onboarding docs\n * Notion pages no one wants to maintain\n\nThe problem isn’t lack of documentation.\n\nIt’s that documentation doesn’t run.\n\nToday we’re shipping SOP Upload, a way to turn existing process documentation directly into executable browser automation.\n\n\nThe gap between “written” and “working”\n\nTraditional automation tools assume you’ll start from scratch:\n\n * re-specify t"
slug: "launch-week-day-4-sop-upload-to-build-new-workflows"
publicationState: "published"
publishedAt: "2026-01-29T16:00:13.000Z"
updatedAt: "2026-01-29T16:00:13.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/a012dba86401001e78803be2ac53520d456b22345ace104a07082de6d29c2772-image-5-1.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Most companies already know how their work gets done.\n\nIt’s written down somewhere:\n\n * internal SOPs\n * runbooks\n * onboarding docs\n * Notion pages no one wants to maintain\n\nThe problem isn’t lack of documentation.\n\nIt’s that documentation doesn’t run.\n\nToday we’re shipping SOP Upload, a way to turn"
---
Most companies already know how their work gets done.

It’s written down somewhere:

-   internal SOPs
-   runbooks
-   onboarding docs
-   Notion pages no one wants to maintain

The problem isn’t lack of documentation.

It’s that documentation doesn’t _run_.

Today we’re shipping **SOP Upload**, a way to turn existing process documentation directly into executable browser automation.



<h3 id="the-gap-between-%E2%80%9Cwritten%E2%80%9D-and-%E2%80%9Cworking%E2%80%9D"><strong>The gap between “written” and “working”</strong></h3>



Traditional automation tools assume you’ll start from scratch:

-   re-specify the process
-   translate steps into configuration
-   debug mismatches between docs and reality

That’s wasteful. The intent already exists. The steps already exist. The missing piece is a system that can map human-written process descriptions to real execution.

SOP Upload is that bridge.



<h3 id="how-it-works"><strong>How it works</strong></h3>



You upload an SOP—anything from a structured runbook to a messy internal doc—and Skyvern uses it as the source of truth for building a task.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d8e6699445069168319675445f04ef9570b98f1471722dad361736354e812af1-image-6.png" class="kg-image" alt="" loading="lazy" width="672" height="429" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/6ce1c287aa9fe2c9a665e4cf0d559817ee25e5d0a8801ec94143828c52cb153c-image-6.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/d8e6699445069168319675445f04ef9570b98f1471722dad361736354e812af1-image-6.png 672w"></figure>



The system extracts:

-   the navigation goal
-   the data extraction goal
-   required inputs and constraints

From there, it produces a runnable automation that can be executed, iterated on, and maintained like any other Skyvern workflow.

This isn’t static parsing. The SOP becomes a living artifact that drives automation behavior.



<h3 id="why-this-matters-in-practice"><strong>Why this matters in practice</strong></h3>



In real organizations, automation rarely fails because people don’t know _what_ to do.

It fails because translating “what we do” into “what the system runs” is slow, brittle, and expensive.

SOP Upload shortens that path:

-   less re-specification
-   less back-and-forth between ops and engineering
-   faster time from documented process to working automation

It’s especially useful for workflows that are:

-   already standardized
-   repeated across teams
-   painful but well understood

In other words: the exact workflows that should be automated first, but usually aren’t.



<h3 id="a-different-mental-model-for-automation"><strong>A different mental model for automation</strong></h3>



This shifts automation from:

> “build a workflow”

to:

> “operationalize what we already wrote down”

Over time, that means SOPs stop being stale documentation and start becoming executable interfaces between humans and systems.

We think that’s a more realistic way automation actually gets adopted inside companies.
