---
title: "Launch Week Day 4: Improved context parameters (+ a story about user pain)"
description: null
excerpt: "What a context parameter is + why we need to update how they work (how to follow the user pain)\n\nLast week we launched support for 6 new workflow blocks including things like “navigation block” and “agent block”.\n\nAnd now we have 2 major updates:\n\n1. You can now define parameters for them with the ubiquitous {{ parameter }} notation\n2. You can now understand that you can define parameters with that notation by clicking a handy little + icon\n\nYou might be wondering.. why did we launch this featur"
slug: "launch-week-day-4-improved-context-parameters-a-story-about-user-pain"
publicationState: "published"
publishedAt: "2024-12-12T16:09:36.000Z"
updatedAt: "2024-12-12T16:09:36.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/2f89e63dac289a8c27abcd813980d312a61bf813b29d4919d814ffd50b10bc87-screen-shot-2024-12-12-at-102042.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "What a context parameter is + why we need to update how they work (how to follow the user pain)\n\nLast week we launched support for 6 new workflow blocks including things like “navigation block” and “agent block”.\n\nAnd now we have 2 major updates:\n\n1. You can now define parameters"
---
What a context parameter is + why we need to update how they work (how to follow the user pain)<br><br>Last week we launched support for 6 new workflow blocks including things like “navigation block” and “agent block”.<br><br>And now we have 2 major updates:<br><br>1. You can now define parameters for them with the ubiquitous {{ parameter }} notation<br>2. You can now understand that you can define parameters with that notation by clicking a handy little + icon <br><br>You might be wondering.. why did we launch this feature? <br><br>Well .. get ready for scrappy startup story time.<br><br>When we first launched our workflows feature, we realized pretty quickly that users need to be able to define their own parameters to generate any value out of it (obvious).<br><br>But we had no way for them to reference attributes within these parameters. Think of cases like.. you want to loop over every invoice you find extract from within a page. <br><br>We quickly shipped this thing called “context parameters” that allowed you to build a configuration to reference them<br><br>Then we followed Garry Tan's advice. Ask a user to use your product and watch them build it<br><br>Every single user we watched went through the same hell: “how do I reference these parameters?” “why is it so confusing?”. It was painful. <br><br>So.. we fixed it. We launched the ability for people to define these parameters using the ubiquitous {{ parameter }} notation<br><br>Then we followed Garry's playbook again. Asked users to use this. And suddenly.. they’re all like “hey how do I add parameters?”. Wow. Turns out no one reads little hover texts to understand how to do it<br><br>So.. we added a little + button on the side of the text box as a way to prompt the user to click on it. And it worked! Other workflow automation tools (ie Zapier) had the same pattern, and so people found it intuitive to navigate this change<br><br>Lesson learned here: Ask users to use your new UX and if it makes you cringe … fix it!
