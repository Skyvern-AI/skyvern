---
title: "The fundamental difference between Engineering and Data Science"
description: null
excerpt: "I spent 4 years before starting Skyvern working at the intersection of Engineering and Data science. I had the honour of building the Search systems for 2 companies: Faire and Gopuff.\n\nI learned something very important in this journey: Data Science and Engineering are fundamentally different fields.\n\nHow? Engineering problems tend to have a clearly defined problem and solution. They are deterministic in nature, and by virtue of that determinism have a solution that can solve that problem. Do yo"
slug: "the-fundamental-difference-between-engineering-and-data-science"
publicationState: "published"
publishedAt: "2025-01-08T15:46:45.000Z"
updatedAt: "2025-01-08T15:46:44.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/5a505d1fe28d5349f93369b9441de862b2b9ac8d89ebf6089111dd07aea28b88-screen-shot-2025-01-05-at-023031.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "I spent 4 years before starting Skyvern working at the intersection of Engineering and Data science. I had the honour of building the Search systems for 2 companies: Faire and Gopuff.\n\nI learned something very important in this journey: Data Science and Engineering are fundamentally different fields.\n\nHow? Engineering problems"
---
I spent 4 years before starting Skyvern working at the intersection of Engineering and Data science. I had the honour of building the Search systems for 2 companies: Faire and Gopuff.<br><br>I learned something very important in this journey: Data Science and Engineering are fundamentally different fields. <br><br>How? Engineering problems tend to have a clearly defined problem and solution. They are deterministic in nature, and by virtue of that determinism have a solution that can solve that problem. Do you want to look some information up? Clear how to do it. Do you want to run some computation? Clear. <br><br>Data science problems are a little bit tricky. They are probabilistic in nature -- the opposite of deterministic. <br><br>This means that single solutions often don't exist. So you need to think about them in terms of probabilities such as confidence intervals (ie p-values), or represent the success / failures of your solution with quantitative numbers such as F-scores or Precision / Recall metrics.<br><br>This distinction is especially important in the age of LLMs. Engineers are being exposed to really capable probabilistic models, and spend so much effort trying to wrangle them to do a specific task such as answer support tickets or browse the web.<br><br>If you treat these problems as deterministic problems, you'll find yourself in edge-case hell, solving each issue one at a time.<br><br>But.. if you treat it like a data science problem.. you can run your solution N times, and represent the outputs along a curve to understand what your solution must look like to get a accurate enough solution<br><br>TL;DR? Evals are important when working on non-deterministic things<br>
