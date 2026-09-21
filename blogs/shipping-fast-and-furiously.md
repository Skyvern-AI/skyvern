---
title: "Shipping Fast and Furiously"
description: null
excerpt: "How do you know you built something people wanted?\n\nWhen you get users start using it and complaining before you even have a chance to announce its launch\n\nThis has happened at Skyvern many times... but the most recent is the most fascinating\n\nWe secretly launched support for credentials almost 2 weeks ago. As soon as the deploy went live, we got a message: \"Hey this isn't working\".\n\nWe had a ton of bugs but we figured we could fix them before people noticed they were there..\n\n 1. The error mess"
slug: "shipping-fast-and-furiously"
publicationState: "published"
publishedAt: "2025-03-11T14:11:03.000Z"
updatedAt: "2025-03-11T14:11:02.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/75a3f40c9508f2af446e9a4770df3544ff069bd007c7b4abec88c8439ce5ef76-nov-22-move-fast-break-things.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "How do you know you built something people wanted?\n\nWhen you get users start using it and complaining before you even have a chance to announce its launch\n\nThis has happened at Skyvern many times... but the most recent is the most fascinating\n\nWe secretly launched support for credentials almost"
---
How do you know you built something people wanted?

When you get users start using it and complaining before you even have a chance to announce its launch

This has happened at Skyvern many times... but the most recent is the most fascinating

We secretly launched support for credentials almost 2 weeks ago. As soon as the deploy went live, we got a message: "Hey this isn't working".

We had a ton of bugs but we figured we could fix them before people noticed they were there..

1.  The error messages were cryptic at best
2.  Two users couldn't simultaneously add credentials (lol)
3.  The adding logic itself was flaky and would error out (lol)
4.  Adding them into a login block in Workflows was non-trivial (required 2 interactions)
5.  There was no indication that you COULD add them... so people just saw an empty dropdown sometimes

The beautiful thing about software is.. you can ship fixes 5 minutes later!



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/734885e9ae168b11c1aee27427112268c621663e252f7607c5093c348c32fbb1-image.png" class="kg-image" alt="" loading="lazy" width="161" height="430"></figure>
