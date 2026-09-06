---
title: "Changelog - July 23 - GPT-4O Mini, Shadow DOM, CSV Support and more!"
description: null
excerpt: "GPT-4O-Mini\n\nWe just launched GPT-4O-Mini support in Open Source Skyvern 🎉\n\nOpenAI's hottest new model at a low low price-point of $0.15 / 1M input tokens.. compared to $5 / 1M input tokens for GPT-4O.\n\nhttps://github.com/Skyvern-AI/skyvern/commit/ec5a0a03c094bfefb5fb810cefff94935532c9e2\n\n\n\n\nRetry Tasks\n\n📃Hot off the press – you can now retry tasks within Skyvern! 🔁\n\nRan a task, loved how it went, and just want to see the magic again?\n\nRan a task, saw it failed, and want to tweak some paramet"
slug: "changelog-july-23-gpt-4o-mini-shadow-dom-csv-support-and-more"
publicationState: "published"
publishedAt: "2024-07-23T14:00:46.000Z"
updatedAt: "2024-07-23T14:00:46.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/b969b0856fd432f44729c1947d554cbb9b1c1a4ca2715c0833a8c34901b13289-main-qimg-baba3194cf7f12d649cb81c72e83f26c-lq.jpg"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "GPT-4O-Mini\n\nWe just launched GPT-4O-Mini support in Open Source Skyvern 🎉\n\nOpenAI's hottest new model at a low low price-point of $0.15 / 1M input tokens.. compared to $5 / 1M input tokens for GPT-4O.\n\nhttps://github.com/Skyvern-AI/skyvern/commit/ec5a0a03c094bfefb5fb810cefff94935532c9e2\n\n\n\n\nRetry Tasks"
---
<h1 id="gpt-4o-mini">GPT-4O-Mini</h1>



We just launched GPT-4O-Mini support in Open Source Skyvern 🎉

OpenAI's hottest new model at a low low price-point of $0.15 / 1M input tokens.. compared to $5 / 1M input tokens for GPT-4O.



<p><a href="https://github.com/Skyvern-AI/skyvern/commit/ec5a0a03c094bfefb5fb810cefff94935532c9e2" rel="noreferrer">https://github.com/Skyvern-AI/skyvern/commit/ec5a0a03c094bfefb5fb810cefff94935532c9e2</a></p>





<h1 id="retry-tasks">Retry Tasks</h1>



📃Hot off the press – you can now retry tasks within Skyvern! 🔁

Ran a task, loved how it went, and just want to see the magic again?

Ran a task, saw it failed, and want to tweak some parameters?

Ran a task, watched it run perfectly, want to hook it up via API?

You can now click "Re-run task" to set up and execute a task again, all parameters filled in 😄



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/986ec550431648c22ef8a3a4db3a0e25fead0117b67d55a6bc6898884cf6a589-screenshot-2024-07-15-at-00-13-03.png" class="kg-image" alt="" loading="lazy" width="2000" height="463" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/7ef6ad832ef72448fab3b53df2cccb563f0ba1faa3fdea0f1a55ebf0f13ba1ee-screenshot-2024-07-15-at-00-13-03.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/b7600de7165df9a84adc523d432fc6f8129921aa1c0cf2fdb38ce5e397d8e5eb-screenshot-2024-07-15-at-00-13-03.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/d5eef7c84f8d96cebc1829d7a114b37b1f0f885ffe85d4c18c7f4e7ca46a01f2-screenshot-2024-07-15-at-00-13-03.png 1600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/986ec550431648c22ef8a3a4db3a0e25fead0117b67d55a6bc6898884cf6a589-screenshot-2024-07-15-at-00-13-03.png 2000w" sizes="(min-width: 720px) 720px"></figure>





<h1 id="shadow-dom">Shadow DOM</h1>



Skyvern now supports interacting with elements inside of a 😎SHADOW DOM😎

What are Shadow DOMs you might ask? Well I had no idea either. We had customers reporting issues with Skyvern's behaviour on some websites.. and it turns out they were all related to Shadow DOMs.

If you're familiar with iframes, Shadow DOMs are another way of essentially doing the same thing, with one little-known difference: Shadow DOMs make styling more consistent when widgets are imported into a bunch of different websites with different stylings

Why does this matter? Well, most of the times it doesn't. Most websites use iframes to load sub-documents inside documents.



<p>Want to learn more? This blog post covers the topic in more detail: <a href="https://glazkov.com/2011/01/14/what-the-heck-is-shadow-dom/" rel="noreferrer">https://glazkov.com/2011/01/14/what-the-heck-is-shadow-dom/</a></p>





<h1 id="csv-support-within-workflows">CSV Support within Workflows</h1>



📋 CSV Support within Skyvern 📋

Ever wanted to automate a bunch of tasks, each with its own row in a CSV?

Example: Go to 10,000 different websites and fill out the contact us form<br>Example: Go to a single website and generate insurance quotes for these 1000 people<br>Example: Go to these websites and download these invoices

That's now possible thanks to Kerem and Salih. They just rolled out support for CSVs within Skyvern's Workflows feature.. which lets companies feed Skyvern inputs from CSVs!

Want to learn more? Check out the [documentation here](https://docs.skyvern.com/workflows/workflow-blocks#fileparserblock)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/5c376013345468ab3acf2caa154d0fb2b51cbe77b89fad7e66cbda178419de75-image.png" class="kg-image" alt="" loading="lazy" width="2000" height="836" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/57dadecc9735fbb772a76c08bc1a0c12d4f028713b47d7a35f23eaf405f6fbfc-image.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/5793f55fc77ddc87cb8c4e3e09d4c4740844c99174fed42c8078ccaeafd5c90b-image.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/3205e48b7d3e3d0131b186360e2151779d816d2ff5f3b4222f8ac47ca8ed389c-image.png 1600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/5c376013345468ab3acf2caa154d0fb2b51cbe77b89fad7e66cbda178419de75-image.png 2182w" sizes="(min-width: 720px) 720px"></figure>





<h1 id="totp-support">TOTP Support</h1>



🗝️ Need Skyvern to log into websites with TOTPs? Don't worry – we've got you covered!

We already supported QR-code based TOTPs, but email / SMS based TOTPs were the bane of our existence.. until stepped in.

Shu just launched a killer feature: TOTPs within Skyvern. You can now give Skyvern a TOTP retrieval URL, and when Skyvern is presented with a screen asking for a 2FA code, it'll call that URL to grab the code and automate whatever is behind that paywall!



<p>Want to learn more? Read our advanced feature documentation here: <a href="https://docs.skyvern.com/running-tasks/advanced-features#time-based-one-time-password-totp" rel="noreferrer">https://docs.skyvern.com/running-tasks/advanced-features#time-based-one-time-password-totp</a></p>





<h1 id="meme-alert">Meme Alert</h1>





<figure class="kg-card kg-image-card"><img src="https://media.licdn.com/dms/image/C5612AQHOdCjZPyDRew/article-cover_image-shrink_600_2000/0/1636042355510?e=2147483647&amp;v=beta&amp;t=2FXHrbwT-W3BFQxbs6EvMYQTG1Rfd2xpZi6WaGikFjY" class="kg-image" alt="What's Driving Healthcare Automation?" loading="lazy" width="375" height="249"></figure>





<h1 id=""></h1>
