---
title: "How we accidentally burned through 200GB of proxy bandwidth in 6 hours"
description: null
excerpt: "Context\n\nSkyvern is an AI agent that helps companies automate workflows in the browser. We run leverage proxy networks and run headful browser instances in the cloud to facilitate most of our automations.\n\n😱 200GB of proxy bandwidth was approximately $500 burned over the course of 6 hours\n\nOne fatal morning\n\nI woke up one morning to an alarm on my phone – Skyvern's failure rate was through the roof. Classic startup moment.\n\nLooking through our alerts, I noticed that our proxy bandwidth alert ha"
slug: "how-we-accidentally-burned-through-200gb-of-proxy-bandwidth-in-6-hours"
publicationState: "published"
publishedAt: "2024-09-18T14:10:42.000Z"
updatedAt: "2024-09-18T14:10:41.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/5bd3ede6b9936bb8108a0b63d2b299072396691f38276453f568312104b471b0-ghost-admin-image.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Context\n\nSkyvern is an AI agent that helps companies automate workflows in the browser. We run leverage proxy networks and run headful browser instances in the cloud to facilitate most of our automations.\n\n😱 200GB of proxy bandwidth was approximately $500 burned over the course of 6 hours\n\nOne fatal morning"
---
**Context**

Skyvern is an AI agent that helps companies automate workflows in the browser. We run leverage proxy networks and run headful browser instances in the cloud to facilitate most of our automations.

😱 200GB of proxy bandwidth was approximately $500 burned over the course of 6 hours

**One fatal morning**

I woke up one morning to an alarm on my phone – Skyvern's failure rate was through the roof. Classic startup moment.

Looking through our alerts, I noticed that our proxy bandwidth alert had fired off. That's weird – we just renewed our plan a few days ago, and should have enough quota to last a month.

I took a look at our metrics and saw something that made my heart sink – we just burned through 200GB of proxy bandwidth in the last 6 hours. HOW DID THAT HAPPEN?



<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/5bd3ede6b9936bb8108a0b63d2b299072396691f38276453f568312104b471b0-ghost-admin-image.png" class="kg-image" alt="" loading="lazy" width="1600" height="308" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/91d7011178b03d061d659c68d95ae27cfd4a80028435375752a371c371fc6af4-image-2.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/85bbc75438dcba83eeba94a7f050413719b24d04b3c18dead69422f62344a0b1-image-2.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/5bd3ede6b9936bb8108a0b63d2b299072396691f38276453f568312104b471b0-ghost-admin-image.png 1600w" sizes="(min-width: 720px) 720px"><figcaption><span style="white-space: pre-wrap;">Proxy bandwidth going through the roof</span></figcaption></figure>



My first thought was – did we get compromised? We give all new users $5 of credits to play around with.. and have had users try to abuse it before. Did someone figure out how to create millions of accounts and burn our bandwidth?

**Digging deeper**

Taking a quick look at our usage stats.. no.. nothing out of the ordinary.

OK. Let's dig a little bit deeper – where is this bandwidth going?



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/89bd5d1bc4d7ce762b23ba91a3bc78ce4fdb3ad16ee6de02d01b9b8b449a904a-image-3.png" class="kg-image" alt="" loading="lazy" width="2000" height="1001" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/34222fc7a0f35bfa525f8ac8d1f8c468aee821ae68af6123036ddbf1efa12f33-image-3.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/a48b7c29641a06ccf3bfd200acb9d36c0072b0be57c5ac9b5f5349f15a0f3e4a-image-3.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/3e8df2dce30e8927620b4f76819864a8dd6c894b780a29cb5140f1c285b2acd4-image-3.png 1600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/89bd5d1bc4d7ce762b23ba91a3bc78ce4fdb3ad16ee6de02d01b9b8b449a904a-image-3.png 2000w" sizes="(min-width: 720px) 720px"></figure>



What's going on? Why is there a random call to <a href="http://optimizationguide-pa.googleapis.com/" rel="noopener noreferrer">optimizationguide-pa.googleapis.com</a>[11:05](https://skyvern.slack.com/archives/C05B62MHYKW/p1722006341046559?thread_ts=1722005282.450889&cid=C05B62MHYKW) occupying 70MB of bandwidth.. over and over and over again?

Doing a quick google search revealed this thread:



<p><a href="https://www.reddit.com/r/chrome/comments/truz5d/the_chrome_downloads_for_no_reason_whatsoever/" rel="noopener noreferrer">https://www.reddit.com/r/chrome/comments/truz5d/the_chrome_downloads_for_no_reason_whatsoever/</a></p>



and



<p><a href="https://support.google.com/chrome/thread/157884177/chrome-appearing-to-download-without-me-downloading-anything?hl=en" rel="noopener noreferrer">https://support.google.com/chrome/thread/157884177/chrome-appearing-to-download-without-me-downloading-anything?hl=en</a></p>



OK this is starting to make sense. Google's downloading a machine learning model in the background to cache, and we seem to be downloading it over and over again

We don't currently persist browser state between sessions.. did we end up in a state where google wants to cache the model, and we keep resetting to the uncached version?

**Potential Solution**

We have a few options to fix this:

#1 - Run chrome locally and save the locally generated `user_data_dir` which includes the model, effectively caching it.

Problem: if the model became stale it would re-trigger the download again in the future

#2 - Introduce a rule into our sidecar proxy to block this specific google URL – prevent Google from downloading the model in the first place



<!--kg-card-begin: html-->
<table class=" diff-table js-diff-table tab-size  file-diff-split js-file-diff-split" data-tab-size="8" data-diff-anchor="diff-29503a243bc2a2bd3d208180ad4ddd8f58626fdf8aa94e86fce39e36dcabaef6" data-paste-markdown-skip="" style="box-sizing: border-box; border-spacing: 0px; border-collapse: separate; width: 1854px; table-layout: fixed; tab-size: 8;"><tbody style="box-sizing: border-box;"><tr data-hunk="2b8a444c136678ed358c3fc90a9f64d496fb0fd2276766fd4fe373ac7f1fa137" style="box-sizing: border-box;"><td data-split-side="right" data-lock-side-selection="" class="code-review blob-code blob-code-addition js-file-line is-hovered" style="box-sizing: border-box; padding: 0px 10px 0px 22px; position: relative; line-height: 20px; vertical-align: top; background-color: var(--diffBlob-addition-bgColor-line, var(--color-diff-blob-addition-line-bg)); outline: transparent dotted 1px;"><span class="blob-code-inner blob-code-marker js-code-nav-pass" data-code-marker="+" style="box-sizing: border-box; display: table-cell; overflow: visible; font-family: var(--fontStack-monospace, ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace); font-size: 12px; color: var(--fgColor-default, var(--color-fg-default)); overflow-wrap: anywhere; white-space: pre-wrap;"><span class="x x-first" style="box-sizing: border-box; border-top-left-radius: 0.2em; border-bottom-left-radius: 0.2em; color: var(--diffBlob-addition-fgColor-text, var(--color-diff-blob-addition-fg)); background-color: var(--diffBlob-addition-bgColor-word, var(--color-diff-blob-addition-word-bg));">  - </span><span class="pl-s x x-last" style="box-sizing: border-box; color: var(--diffBlob-addition-fgColor-text, var(--color-diff-blob-addition-fg)); border-top-right-radius: 0.2em; border-bottom-right-radius: 0.2em; background-color: var(--diffBlob-addition-bgColor-word, var(--color-diff-blob-addition-word-bg));">DOMAIN-SUFFIX,optimizationguide-pa.googleapis.com,REJECT</span></span></td></tr></tbody></table>
<!--kg-card-end: html-->



**Going back to sleep**

We decided to do both to solve this problem. We updated our `user_data_dir` to get a quick fix out, and also updated our sidecar proxy to block that specific URL from triggering downloads in the future
