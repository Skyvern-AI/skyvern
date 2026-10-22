---
title: "Handling inveitable failures in a startup"
description: null
excerpt: "We have had a few interesting issues recently\n\n1. We were helping one of our customers fill out some tax forms, and Skyvern failed to interact with the \"yes\" and \"no\" radio buttons because clicking the labels didn't work -- very unusual for websites\n2. We were helping another customer fill out some permit applications, and Skyvern failed to find the correct address and ran into an issue\n\nThis got us to an interesting series of conversations with our customers -- what kind of success / failure ra"
slug: "untitled-2"
publicationState: "published"
publishedAt: "2024-11-18T15:21:36.000Z"
updatedAt: "2024-11-18T15:21:35.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/60c6e67be5dfe0c73605cecc2042022fe8433a9ff2b5ed04ef515104f21610d9-screen-shot-2024-10-21-at-143712.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "We have had a few interesting issues recently\n\n1. We were helping one of our customers fill out some tax forms, and Skyvern failed to interact with the \"yes\" and \"no\" radio buttons because clicking the labels didn't work -- very unusual for websites\n2. We were helping another customer"
---
We have had a few interesting issues recently<br><br>1. We were helping one of our customers fill out some tax forms, and Skyvern failed to interact with the "yes" and "no" radio buttons because clicking the labels didn't work -- very unusual for websites<br>2. We were helping another customer fill out some permit applications, and Skyvern failed to find the correct address and ran into an issue<br><br>This got us to an interesting series of conversations with our customers -- what kind of success / failure rate should be expected from AI agents?<br><br>Obviously, every customer wants 100% success rate for everything, but if you read the fine print for any software product (ie AWS), they charge an arm and a leg for anything above 99.999% uptime / success.<br><br>AI Web agents introduce another class of possible software issues -- Skyvern uptime isn't enough.. the underlying website must also be up, functional and without error. <br><br>How can we guarantee correctness to our customers?<br><br>Should we promise 99% uptime, but then add small text at the bottom indicating that it's only uptime as dictated by us?<br>Should we <br><br>Well --> It turned out to be pretty straightforward. <br><br>We just talked to the customer to better understand their risk profile (+ expectations around successes and failures)<br><br>And in talking to the customer, we built trust. This is what customers are really looking for when they're trying to get to a specific tier of uptime<br><br>That being said, we haven't turned this into a formula just yet<br><br>How do you build trust with your customers?<br>
