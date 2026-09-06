---
title: "Changelog - November Part 2 - Quality of Life"
description: null
excerpt: "Faster workflow startup times\n\nFaster workflow startup times\n\nUsually performance fixes are a big meme. You get an app update and the release notes just say \"performance improvements\"... but WHAT DOES THAT MEAN?\n\nWe make our performance improvements as obvious as they can get\n\nAny time I did a live demo.. we'd wait 1 minute for a workflow to start\nAny user that wanted to test their workflow.. would wait 1 minute for a workflow to start\nAny customer calling our API.. experienced an unnecessary mi"
slug: "changelog-november-p2-quality-of-life"
publicationState: "published"
publishedAt: "2024-11-15T15:41:50.000Z"
updatedAt: "2024-11-15T15:41:49.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/37dc7f473d790338b3a82ed88d8ddd4f67ab03e8113bff5ee5f3e08d023aac3b-image-6.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Faster workflow startup times\n\nFaster workflow startup times\n\nUsually performance fixes are a big meme. You get an app update and the release notes just say \"performance improvements\"... but WHAT DOES THAT MEAN?\n\nWe make our performance improvements as obvious as they can get\n\nAny time I did a live"
---
<h2 id="faster-workflow-startup-times">Faster workflow startup times</h2>



Faster workflow startup times <br><br>Usually performance fixes are a big meme. You get an app update and the release notes just say "performance improvements"... but WHAT DOES THAT MEAN?<br><br>We make our performance improvements as obvious as they can get<br><br>Any time I did a live demo.. we'd wait 1 minute for a workflow to start<br>Any user that wanted to test their workflow.. would wait 1 minute for a workflow to start<br>Any customer calling our API.. experienced an unnecessary minute of latency<br><br>This was something so painful for our users we had to get it out



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/37dc7f473d790338b3a82ed88d8ddd4f67ab03e8113bff5ee5f3e08d023aac3b-image-6.png" class="kg-image" alt="" loading="lazy" width="290" height="339"></figure>





<h1 id="real-time-actions">Real time actions</h1>



Hot off the press: Real time actions<br><br>No one asked us for this feature.. but.. every time I demo'd Skyvern, we'd watch the livestream take 4-5 actions in a row, then see a burst of actions appear on the right side<br><br>Why did this happen? Well.. we batch actions by page, and it was easier to just show them to users that way.. after they were all done executing<br><br>But.. I could tell. Each time this happened, the person I was demo-ing to looked bored<br><br>Why wouldn't they be bored? I was bored waiting for it to execute! I want faster feedback!!<br><br>So we re-did how we handle actions. We update actions in the UI as soon as they happen.. and it brings back the magic<br><br>Who knew demos + UX was all about focusing on the magic?



<figure class="kg-card kg-video-card kg-width-regular" data-kg-thumbnail="https://blog.skyvern.com/content/media/2024/10/real-time-action-announcement--2--1_thumb.jpg" data-kg-custom-thumbnail=""><div class="kg-video-container"><video src="https://dcbllm8dvghjo.cloudfront.net/media/blog/video/b6516c539480b05d9bda2b0e34f6688ca2d4b58e0826d8296c6c3592b8054d93-real-time-action-announcement-2-1.mp4" poster="https://dcbllm8dvghjo.cloudfront.net/media/blog/e9e816119da85d66ff4c9d9a36726acfcf659d9d87f8a97289cd2c8b1c12573f-spacer.png" width="1298" height="720" loop="" autoplay="" muted="" playsinline="" preload="metadata" style="background: transparent url('https://dcbllm8dvghjo.cloudfront.net/media/blog/09c1f3a73192092a0fc5414ee81a794c560130ef1a3351721b3d5ebb05bf1854-real-time-action-announcement-2-1-thumb.jpg') 50% 50% / cover no-repeat;"></video><div class="kg-video-overlay"><button class="kg-video-large-play-icon" aria-label="Play video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M23.14 10.608 2.253.164A1.559 1.559 0 0 0 0 1.557v20.887a1.558 1.558 0 0 0 2.253 1.392L23.14 13.393a1.557 1.557 0 0 0 0-2.785Z"></path></svg></button></div><div class="kg-video-player-container kg-video-hide"><div class="kg-video-player"><button class="kg-video-play-icon" aria-label="Play video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M23.14 10.608 2.253.164A1.559 1.559 0 0 0 0 1.557v20.887a1.558 1.558 0 0 0 2.253 1.392L23.14 13.393a1.557 1.557 0 0 0 0-2.785Z"></path></svg></button><button class="kg-video-pause-icon kg-video-hide" aria-label="Pause video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="3" y="1" width="7" height="22" rx="1.5" ry="1.5"></rect><rect x="14" y="1" width="7" height="22" rx="1.5" ry="1.5"></rect></svg></button><span class="kg-video-current-time">0:00</span><div class="kg-video-time">
                            /<span class="kg-video-duration">0:07</span></div><input type="range" class="kg-video-seek-slider" max="100" value="0"><button class="kg-video-playback-rate" aria-label="Adjust playback speed">1×</button><button class="kg-video-unmute-icon" aria-label="Unmute"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M15.189 2.021a9.728 9.728 0 0 0-7.924 4.85.249.249 0 0 1-.221.133H5.25a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3h1.794a.249.249 0 0 1 .221.133 9.73 9.73 0 0 0 7.924 4.85h.06a1 1 0 0 0 1-1V3.02a1 1 0 0 0-1.06-.998Z"></path></svg></button><button class="kg-video-mute-icon kg-video-hide" aria-label="Mute"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M16.177 4.3a.248.248 0 0 0 .073-.176v-1.1a1 1 0 0 0-1.061-1 9.728 9.728 0 0 0-7.924 4.85.249.249 0 0 1-.221.133H5.25a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3h.114a.251.251 0 0 0 .177-.073ZM23.707 1.706A1 1 0 0 0 22.293.292l-22 22a1 1 0 0 0 0 1.414l.009.009a1 1 0 0 0 1.405-.009l6.63-6.631A.251.251 0 0 1 8.515 17a.245.245 0 0 1 .177.075 10.081 10.081 0 0 0 6.5 2.92 1 1 0 0 0 1.061-1V9.266a.247.247 0 0 1 .073-.176Z"></path></svg></button><input type="range" class="kg-video-volume-slider" max="100" value="100"></div></div></div></figure>





<h2 id="cloning-workflows">Cloning Workflows</h2>



Skyvern now supports cloning workflows you've already built!<br><br>This one came up as a surprise customer request. We're helping a customer automate invoice downloading with Skyvern, and they came across a website that refers to invoices as "statements" -- sounds pretty innocuous right?<br><br>It's not that simple. All the prompts refer to the PDF files as invoices, and they wanted to test if a simple prompt change for that website would work<br><br>What was the simple prompt change? "In this website, any references to statements are actually invoices". <br><br>They wanted to test this change without causing any regressions on their existing pipeliens, so they asked for a simple feature: "can we duplicate workflows to test stuff out?"<br><br>Turns out... the answer was no. So we quickly built the feature and shipped it the next morning to delight them :)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/fefde616d267f5af4667174f4b4f3dfc2084eb434764805629a56e8bf638f0ab-image-3.png" class="kg-image" alt="" loading="lazy" width="285" height="169"></figure>





<h2 id="importing-exporting-workflows">Importing / Exporting workflows</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8d5cb2875d6e4dc68c85949a560d53532f790bca22f0adb886b519f3a9cabadf-image-4.png" class="kg-image" alt="" loading="lazy" width="360" height="199"></figure>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/15bceb703547c61a39aa0074d839bacb01a624b63e17124307563fb269406137-image-5.png" class="kg-image" alt="" loading="lazy" width="356" height="69"></figure>



You can now import / export workflows in Skyvern<br><br>We started thinking.. why stop at cloning workflows? Why not allow users to export / import workflows?<br><br>This is an extension of a customer request. We had a customer request cloning workflows.. but they also mentioned that exporting / importing them would be valuable as they can version control our workflow states<br><br>This seemed like a feature we could ship fast -- we already convert an entire workflow to JSON / YAML.. why not allow customers to download it?<br><br>A few hours later and a delighted customer later.. here we are 😃
