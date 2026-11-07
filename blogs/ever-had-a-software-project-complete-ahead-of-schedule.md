---
title: "Ever had a software project complete ahead of schedule?"
description: null
excerpt: "Ever had a software project complete ahead of schedule?\n\nI've never seen it.\n\nThere's this philsophy in project planning that says: projects will always find a way to fill all of the time allocated towards them. This is natural inertia that exists. When you hear a deadline, you say \"oh ok I'll do X Y Z to hit that deadline\". The deadline becomes the focal point, not the doing of X, or Y, or Z\n\nThis is why we try our best to either: not set deadlines, or set unrealistically fast deadlines at Skyv"
slug: "ever-had-a-software-project-complete-ahead-of-schedule"
publicationState: "published"
publishedAt: "2025-01-03T15:58:49.000Z"
updatedAt: "2025-01-03T15:58:48.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/2222b52d410caddb6cfbbcf872b1e4c2808f405b8e9c5ecef19e3b6d72fb15b6-every-new-project-be-like-v0-3vtlt3cwlrta1.webp"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "Ever had a software project complete ahead of schedule?\n\nI've never seen it.\n\nThere's this philsophy in project planning that says: projects will always find a way to fill all of the time allocated towards them. This is natural inertia that exists. When you hear a deadline, you"
---
Ever had a software project complete ahead of schedule?<br><br>I've never seen it. <br><br>There's this philsophy in project planning that says: projects will always find a way to fill all of the time allocated towards them. This is natural inertia that exists. When you hear a deadline, you say "oh ok I'll do X Y Z to hit that deadline". The deadline becomes the focal point, not the doing of X, or Y, or Z<br><br>This is why we try our best to either: not set deadlines, or set unrealistically fast deadlines at Skyvern<br><br>"Not setting deadlines" is a risky approach to project planning. It only works with intrinsically motivated people (ie "owners") -- the doing of the project is so important that there is nothing else worth doing. We try not to choose this approach often<br><br>"Unrealistically fast deadlines" is a common approach and it's designed to force everyone to think through each project from the lens of an MVP. What's the absolute minimum thing required to make this customer successful. By default, it means additional complexity requires "asking for more time", which creates a platform to discuss the scope of the project. Do we need to refactor this segment of the code to hit this goal? If yes -> let's do it. If no -> Let's defer it til the future. What's the simplest data model that will allow us to hit the goal? -> Let's use that.<br><br>We were working hard towards this goal of launching the contact-form agent within 2 weeks. The first week went by, and we spent most of the time wrapping up other projects and coordinating the design. Then something magical happened. We pulled everything together and launched it in the next few days. <br><br>We were tempted to add scope: Let's redesign the logged-in landing page. Let's re-design the left navbar. Let's redesign the task history page. But there's no way we could hit the deadline if we did those things.. and well.. do we really need to do them to test this hypothesis? No. <br><br>Let's do the simplest thing possible: add a column to the db for this use-case. Create a special landing page + logged in experience. Make small tweaks to our nav bar. <br><br>**Adding a column?** Takes an hour<br>**Creating a special landing page?** A day<br>**Tweaks to the nav bar?** Half a day<br>**Special logged-in landing page?** Half a day<br><br>Suddenly.. we launched something that was going to take 2 weeks in 3 days. All by focusing on the things that really matter and ignoring everything else.<br><br>



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/1012ef07a6dbe36d80c75e3e296ad7dda15b2eb479a770e410f96711a197bc32-image-3.png" class="kg-image" alt="" loading="lazy" width="640" height="720" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/630e7910ccb95dade3b9f81b0dd169d6e74fa3af04e60f8a7141c0e37b00e26b-image-3.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/1012ef07a6dbe36d80c75e3e296ad7dda15b2eb479a770e410f96711a197bc32-image-3.png 640w"></figure>
