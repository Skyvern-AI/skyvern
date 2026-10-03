---
title: "Changelog - September 4 - New pricing page, Faster live streams, and more!"
description: null
excerpt: "New Pricing page\n\nSkyvern's landing page just got a minor update – we now have a dedicated pricing page 💰!\n\nWe have 2 main problems we think we can solve with a pricing page:\n\n#1: Our pricing wasn't clear at all! The only way to learn about our pricing was to log in and see the billing page\n\n#2: Many people coming into Skyvern didn't realize we have an open source version! Our pricing page now makes it clear\n\nWe still have one big question on our minds: should we continue to do usage-based pric"
slug: "changelog-september-4-new-pricing-page-faster-live-streams-and-more"
publicationState: "published"
publishedAt: "2024-09-04T13:15:41.000Z"
updatedAt: "2024-09-04T13:15:41.000Z"
author: "suchintan"
tags: []
featureImage: null
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "New Pricing page\n\nSkyvern's landing page just got a minor update – we now have a dedicated pricing page 💰!\n\nWe have 2 main problems we think we can solve with a pricing page:\n\n#1: Our pricing wasn't clear at all! The only way to learn about our pricing was"
---
<h1 id=""></h1>





<h1 id="new-pricing-page">New Pricing page</h1>



Skyvern's landing page just got a minor update – we now have a dedicated pricing page 💰!

We have 2 main problems we think we can solve with a pricing page:

#1: Our pricing wasn't clear at all! The only way to learn about our pricing was to log in and see the billing page

#2: Many people coming into Skyvern didn't realize we have an open source version! Our pricing page now makes it clear

We still have one big question on our minds: should we continue to do usage-based pricing as it is today ($0.10 / page)? Or should we try out monthly plans ($10 / $50 / $300)? What do you all think?



<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/4a215c07bf107943f1c06e6e248c78719a04ac26bb9b56d28e6b0f626a6c36d9-image-1.png" class="kg-image" alt="" loading="lazy" width="2000" height="938" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/aa42d2d776608f284b68c5de4bfa3763e566851e01118b77d13736d4d0e1c25c-image-1.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/959c5e8ca05a2e3f3a4b3d990742285e637ec59789f11168a92eecafb1374370-image-1.png 1000w, https://dcbllm8dvghjo.cloudfront.net/media/blog/f653e5e2996bb588348bb50751ec6c2207fe0414f144baa19d2ac57308cb6202-image-1.png 1600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/4a215c07bf107943f1c06e6e248c78719a04ac26bb9b56d28e6b0f626a6c36d9-image-1.png 2000w" sizes="(min-width: 720px) 720px"><figcaption><span style="white-space: pre-wrap;">Skyvern pricing page</span></figcaption></figure>





<h1 id="faster-livestream-start-times">Faster livestream start times</h1>



⏲️ We just rolled out a huge change for our self-serve users. You'll now see your tasks start up WAY faster than before

Why did it take so long before?

Well, let me tell you about this scrappy little startup that wanted to launch fast, so they decided to have all of their customers share a single sqs queue for task execution.

Why is that not ideal?

Let's say that this scrappy little startup gets a few power users. And those power users occupy a lot of space in the queue – so much so that all of the browser nodes end up being busy, so new tasks end up waiting in the queue for compute to free up

That's the point of queues? Why not just use priority queues?

This scrappy little startup was using priority queues! But that's unfortunately not enough – even tasks with the highest priority (people waiting for a livestream) have to wait for a browser node to free up, leading to a less-than-ideal user experience

Wow. Who's the hero that saved this scrappy little startup?

Shu came to the rescue and helped all of our users out :)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/ea97d1c82662d6e47d00c683c43ee332267556fe15134042a2e4c3b3ce3428d7-image.png" class="kg-image" alt="" loading="lazy" width="982" height="980" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/501bd0b113ff05904ea4bad30d9fa26094e58ff6b167b7c44e07d85b907cfec0-image.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/ea97d1c82662d6e47d00c683c43ee332267556fe15134042a2e4c3b3ce3428d7-image.png 982w" sizes="(min-width: 720px) 720px"></figure>





<h1 id="task-livestreaming-in-the-open-source-repo">Task Livestreaming in the open source repo</h1>



You thought task livestreaming in <a href="https://app.skyvern.com" rel="noreferrer">Skyvern Cloud</a> was cool? Well we just brought it into our open source repo. Running Open Source Skyvern via our [Github Repo](https://github.com/Skyvern-AI/Skyvern) now lets you see what's going on in the browser instance... in real time 😎



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/a5b658f2a1a7e29fbac9093e0e20bb6aef9dd9bfa481d7d47e6d3f89df07d4e5-oss-livestream.gif" class="kg-image" alt="" loading="lazy" width="600" height="411" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/a5b658f2a1a7e29fbac9093e0e20bb6aef9dd9bfa481d7d47e6d3f89df07d4e5-oss-livestream.gif 600w"></figure>





<h1 id="dynamic-autocompletes">Dynamic Autocompletes</h1>



🚀 This is a huge update. Skyvern can now read, interact with, and understand how to populate dynamic autocompletes even if it's not obvious how to do so.

🧠 Why is this such a big deal?

Skyvern was designed to read the screen and decide what actions to take based on a users' goal. Historically, we struggled with dynamic autocompletes for one of two reasons:

1.  The dynamic autocomplete would be unclear, and we would update the field without making a selection, sometimes failing to submit forms correctly (wrong input)
2.  Skyvern would require a lot of interactions to figure out how to add the information in the correct format (slow + expensive)

💡 Our team invented a new process for handling autocompletes

We broke the problem down into a few phases:

1.  Identify if the field is a dynamic autocomplete by listening to DOM changes
2.  Take the first action on the autocomplete assuming it's a normal text box
3.  Make the correct selection if present
4.  As the dynamic dropdown changes, we involve cheaper LLMs like GPT-4O-Mini to alter the input text to be more or less precise based on the output
5.  Go back to step 2

Through this process, we've improved the quality of our dropdown selection while also cutting down costs over 6x for this particular type of field



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/0391d502832ad10b67c5262c734efdfcb88435d81091379d47656d58e2530b85-e030855512af19d5f8df7d1ee99dfc57-ezgif-com-video-to-gif-converter.gif" class="kg-image" alt="" loading="lazy" width="600" height="376" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/0391d502832ad10b67c5262c734efdfcb88435d81091379d47656d58e2530b85-e030855512af19d5f8df7d1ee99dfc57-ezgif-com-video-to-gif-converter.gif 600w"></figure>





<h1 id="-1"></h1>
