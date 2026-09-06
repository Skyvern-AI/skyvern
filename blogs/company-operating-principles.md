---
title: "Skyvern's operating principles (as they are today)"
description: null
excerpt: "This is a list of operating principles we've observed to work well in our startup.\n\nI imagine this will will get longer and longer as the company scales.. but it's cool to capture and share as it is today\n\n 1. Treat every project like a research project. Be hypothesis driven, and do the minimum work required to test the hypothesis\n    1. Example: Hypothesis: “I believe that removing a confirmation screen before creating a task will yield a better UX”\n       1. Test: Remove the confirmation scree"
slug: "company-operating-principles"
publicationState: "published"
publishedAt: "2024-12-02T15:01:09.000Z"
updatedAt: "2024-12-02T15:01:08.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/1e3268f4fc73d69e7cf3eb540496f600c70a0c314661d0e7fb23c664672dab38-0-9e6oyty50mpik-o.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "This is a list of operating principles we've observed to work well in our startup.\n\nI imagine this will will get longer and longer as the company scales.. but it's cool to capture and share as it is today\n\n 1. Treat every project like a research project. Be"
---
This is a list of operating principles we've observed to work well in our startup.

I imagine this will will get longer and longer as the company scales.. but it's cool to capture and share as it is today

1.  Treat every project like a research project. Be hypothesis driven, and do the minimum work required to test the hypothesis
    1.  Example: Hypothesis: “I believe that removing a confirmation screen before creating a task will yield a better UX”
        1.  Test: Remove the confirmation screen and start the task
        2.  Validation: Talk to a few users / try using it yourself
        3.  Decision: Did it work? Did it not work? Can we measure results?
2.  Move fast and break things.. unless it will take too long to reverse the breakage
    1.  Take a moment (or use your intuition) to understand what the consequence of moving fast would be. Would the beneifts outweigh the risk?
        1.  Ex: I want to rotate our proxy vendor API key. I should just go ahead and do it as rolling it back is a trivial procedure
        2.  Ex: I want to update 1 million database rows. I should get on a call with someone else to review it before I run it because reversing it is really challenging
    2.  Don't forget: you can use your fast movement to un-break things too, so don’t let the fear of breaking things paralyze you
3.  Obey the 15 minute principle (unless waiting 15 minutes would cause more harm than good ie servers are down)
    1.  Try researching the problem for 15 mintues or solving it yourself. At minimum, you’ll educate yourself on the problem and ask really poignant questions. At best, you’ll solve it and not need to interrupt anyone
    2.  Ex: “Wow I hit this issue I’ve never seen before in postgres. Let me ask ChatGPT about this error. Interesting it told me to try running this.. oh shoot it still didn’t work. Let me try this other thing. Hmm.. still running into a wall, let me ping xyz”
4.  Practice extreme ownership over everything you work on. We want you to work on anything with an extreme amount of love and care.
    1.  Startups often have more work to do than there are people to do them, and there are often a multitude of balls in the air that are all about to drop. This means no one has the space to supervise
    2.  Create an environment of high trust and high accountability. Assume people will get the stuff done they said they will get done. Hold them accountable if they don’t.
    3.  Ex: “Customer A needs a demo built.. and while I was building a demo I ran into issue X and Y. I fixed issue X but I don’t know how to fix issue Y, so I’m going to loop the right person in, and maek sure Y gets fixed before the call”
5.  No Acronyms unless they are googlable. Companies fall into the [TLA](https://en.wikipedia.org/wiki/Three-letter_acronym) trap, creating an internal language that’s hard to understand and alienates new-comers
    1.  There will always be internal-only language. We have it here.. codenames for projects. We should minimize it whenever possible.
    2.  Read Elon Musk’s email about acronyms: [https://gist.github.com/klaaspieter/12cd68f54bb71a3940eae5cdd4ea1764](https://gist.github.com/klaaspieter/12cd68f54bb71a3940eae5cdd4ea1764)
