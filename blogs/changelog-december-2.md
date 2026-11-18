---
title: "Changelog - December: Workflow failure reasons, Zapier support, better task icons and more!"
description: null
excerpt: "Workflow Failure reasons\n\nThis was a looooooooong time coming\n\nPreviously, if a Skyvern request failed.. we would just show \"failed\" and you would have to click around to figure out what that is\n\nNow we just show you front and center :)\n\n\nSkyvern x Zapier\n\nExciting news: our integration with Zapier is now live! Zapier is an automation platform tailored to freelancers, small businesses, and non-technical users. We're excited to be one of the 5,000+ supported integrators available, extending your "
slug: "changelog-december-2"
publicationState: "published"
publishedAt: "2024-12-19T15:50:26.000Z"
updatedAt: "2024-12-19T15:50:25.000Z"
author: "suchintan"
tags: []
featureImage: null
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Workflow Failure reasons\n\nThis was a looooooooong time coming\n\nPreviously, if a Skyvern request failed.. we would just show \"failed\" and you would have to click around to figure out what that is\n\nNow we just show you front and center :)\n\n\nSkyvern x Zapier\n\nExciting news: our integration with Zapier"
---
<h2 id=""></h2>





<h2 id="workflow-failure-reasons">Workflow Failure reasons</h2>



This was a looooooooong time coming<br><br>Previously, if a Skyvern request failed.. we would just show "failed" and you would have to click around to figure out what that is<br><br>Now we just show you front and center :)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/b7dcf46739c12a2d8f7edd3b0ed5695f3fb82c7728ff01287a03d0f0373b9d88-image.png" class="kg-image" alt="" loading="lazy" width="1513" height="144" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/daa8b0bf7df58a6ed5aad5cf640dab50be5aa6a5e8209f2c5c8442c395e1c97f-image.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/8bceea62c6d8e172a19a5c6441e3e245bef71b167daf44e394c2c8ecbbfd49e2-image.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/b7dcf46739c12a2d8f7edd3b0ed5695f3fb82c7728ff01287a03d0f0373b9d88-image.png 1513w" sizes="(min-width: 720px) 720px"></figure>





<h2 id="skyvern-x-zapier">Skyvern x Zapier</h2>



Exciting news: our integration with Zapier is now live! Zapier is an automation platform tailored to freelancers, small businesses, and non-technical users. We're excited to be one of the 5,000+ supported integrators available, extending your Zaps into the browser.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/c37774b912bab620be21f141da1dfd5f4717db8d746e1153983bc54e74c5b167-zapierintegration-padding.png" class="kg-image" alt="" loading="lazy" width="1476" height="720" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/427325b47e0712e49c19aa44f1ccf6e1a76044dd436423ee8b3759ee03d8de07-zapierintegration-padding.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/465163935e82c884c716ba27dd489d9b051fc1ec61276979ee4be9296dc13a88-zapierintegration-padding.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/c37774b912bab620be21f141da1dfd5f4717db8d746e1153983bc54e74c5b167-zapierintegration-padding.png 1476w" sizes="(min-width: 720px) 720px"></figure>





<h2 id="example-task-icons">Example Task Icons</h2>



This is the first thing users' see when they log into Skyvern<br><br>We expected people to be able to clearly read and interact with all of the options there<br><br>Yet.. in my demo calls I kept hearing "do you do complex form filling?" or "can you fill out job applications?"<br><br>This meant that even if these examples were visible, they were not obvious to people. They didn't "pop" the right way, or draw people's attention correctly<br><br>So.. we decided to launch a really simple change: We added icons to each thing to really draw your attention to it. <br><br>What do you think of the change?<br>



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/2e74f6ca0e28ce8bd6848f1ed41009eb67661f7e3d56be9ef80dfc90fa9cfbfa-image.png" class="kg-image" alt="" loading="lazy" width="1567" height="508" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/0b01c3880704341783ba01c364e11afc2c747a8663d3d755ef1a43e3a8bea1fc-image.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/6d58fb10310d9478573dd198f042f3e11bdda0b9dc254e0e12efb51f7fdb997d-image.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/2e74f6ca0e28ce8bd6848f1ed41009eb67661f7e3d56be9ef80dfc90fa9cfbfa-image.png 1567w" sizes="(min-width: 720px) 720px"></figure>





<h2 id="workflow-global-parameters">Workflow Global Parameters</h2>



This is a super small change.. but.. it kept irritating our power users!<br><br>We kept hearing the same request over and over again: "How can I save the browser session?" "How can I hard code a proxy location?"<br><br>We actually built these features out in our API... but never got around to adding support via the UI<br><br>After hearing it enough times, here it is! <br><br>You can now:<br>1. Hardcode a webhook callback url at the workflow level<br>2. Hardcode a proxy location at the workflow level<br>3. Persist browser sessions between workflow runs at.. you guessed it.. the workflow level



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/0a311eb37f9d6dc09173997bf896f055703ce660f6835ca88a08b10c17e2a07c-image-1.png" class="kg-image" alt="" loading="lazy" width="677" height="624" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/b7b43620a00b2a602582c0f79ba0c6aececf3bec7422b4dba871b16d97ba3013-image-1.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/0a311eb37f9d6dc09173997bf896f055703ce660f6835ca88a08b10c17e2a07c-image-1.png 677w"></figure>





<h2 id="cancel-workflows">Cancel Workflows</h2>



You wanted to cancel tasks.. And then you wanted to cancel workflows..

So we built it to make sure you don't cancel your relationship with Skyvern



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/c9ae8692b2ecddb6cfd3ea07c33bf247d50b5726fcf516c303cff9c74f5971de-image-2.png" class="kg-image" alt="" loading="lazy" width="419" height="166"></figure>
