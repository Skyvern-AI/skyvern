---
title: "Rustwright: Playwright rewritten in rust that occupies 70% less memory (and is 2.55x faster)"
description: null
excerpt: "Hey everyone, Suchintan from Skyvern (YC S23). We run browser automation for AI agents at scale, and we're launching Rustwright: Playwright on an in-process Rust CDP engine. The engine consumes 70% less memory than Playwright, and is 2.55x faster to boot.\n\nWe built this to improve Skyvern’s memory footprint, and thought this would be independently valuable to the broader community\n\nThe whole idea fits in two lines:\n\nplaywright-python:  \nyour code ──pipe──► Node driver ──CDP──► Chromium\n\nrustwrig"
slug: "rustwright-playwright-rewritten-in-rust-that-occupies-70-less-memory-and-is-2-55x-faster"
publicationState: "published"
publishedAt: "2026-07-15T15:15:11.000Z"
updatedAt: "2026-07-15T15:15:11.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/90b52fa8692dd6f6093d099c958cefe0b0f380049e5cc9ea710df049b83da324-banner.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Hey everyone, Suchintan from Skyvern (YC S23). We run browser automation for AI agents at scale, and we're launching Rustwright: Playwright on an in-process Rust CDP engine. The engine consumes 70% less memory than Playwright, and is 2.55x faster to boot.\n\nWe built this to improve Skyvern’"
---
Hey everyone, Suchintan from Skyvern (YC S23). We run browser automation for AI agents at scale, and we're launching [Rustwright](https://github.com/Skyvern-AI/rustwright): [Playwright on an in-process Rust CDP engine.](https://github.com/Skyvern-AI/rustwright) The engine consumes 70% less memory than Playwright, and is 2.55x faster to boot.

**We built this to improve Skyvern’s memory footprint**, and thought this would be independently valuable to the broader community

The whole idea fits in two lines:



<pre><code>playwright-python:  
your code ──pipe──► Node driver ──CDP──► Chromium

rustwright:         
your code ───────── raw CDP ───────────► Chromium
</code></pre>



Removing the driver changes three things:

1.  <strong>Less abstraction.</strong> The driver subprocess and its pipe speaks CDP straight to Chromium. In our runs, the new engine is 2.55× and **consumes 70% less memory** than the equivalent playwright process
2.  <strong>No Playwright driver fingerprint.</strong> The driver never loads, so its signatures never appear: no `__playwright__binding__` globals, no `Runtime.enable` on the default path (the well-known console-serialization leak). The claim is deliberately narrow: no Playwright-specific automation fingerprint, not "undetectable"
3.  <strong>A single rust core</strong>: a single Rust core backs both language bindings (Python with sync and async; Typescript) with plans to expand to many popular languages (Ruby / Go / Java to name a few). Within the supported surface, migration is often a one-line import change — `from rustwright.sync_api import sync_playwright.`Clicks and typing go through real CDP input events, not synthetic DOM calls, and cross-origin iframes auto-attach with `frame_locator()` routing across origins.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/b7863247bd38e98b6ad69eb55b53646c06d2bd860598e04bcd07e07f58c9dfc9-rustwright-vs-playwright-1.gif" class="kg-image" alt="" loading="lazy" width="720" height="960" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/b7863247bd38e98b6ad69eb55b53646c06d2bd860598e04bcd07e07f58c9dfc9-rustwright-vs-playwright-1.gif 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/b7863247bd38e98b6ad69eb55b53646c06d2bd860598e04bcd07e07f58c9dfc9-rustwright-vs-playwright-1.gif 720w" sizes="(min-width: 720px) 720px"></figure>



[playwright-python](https://github.com/microsoft/playwright-python) pipes every call through a bundled Node driver process. [Rustwright](https://github.com/Skyvern-AI/rustwright) drives Chromium over raw Chrome DevTools Protocol from a Rust core, an async CDP client on Tokio (WebSocket, plus opt-in Unix-pipe transport), exposed in-process through thin PyO3 bindings for Python and napi-rs bindings for Node.

Status: alpha, Chromium-only

**Want to give it a try? Change one line of code!**



<pre><code class="language-jsx">pip install rustwright

- from playwright.sync_api import sync_playwright
+ from rustwright.sync_api import sync_playwright
</code></pre>





<p>⭐&nbsp;Let us know what you think: <a href="https://github.com/Skyvern-AI/rustwright">https://github.com/Skyvern-AI/rustwright</a></p>
