---
title: "Learning the hard way: AWS EC2 /tmp/ folder is always loaded into memory"
description: null
excerpt: "We fixed a memory leak in our codebase\n\nIt's rare for companies to talk about backend changes.. but this one is super important\n\nWe kept having issues where our customers' tasks would randomly die. We had no idea what was going on\n\n1. We did running profiles of our EC2 instances and noticed that memory was spiking right before a worker would die... how interesting\n2. We also knew one small quirk about our product: we would save all screenshots / llm calls / downloaded files to the /tmp/ director"
slug: "learning-the-hard-way-aws-ec2-tmp-folder-is-always-loaded-into-memory"
publicationState: "published"
publishedAt: "2024-12-30T14:37:51.000Z"
updatedAt: "2024-12-30T14:37:52.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/91e339dff7447e2c99d081c5dfce2cb802278d3eac7ec7ec1e9249893e61cd5a-i0era9fzm8z31.webp"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "We fixed a memory leak in our codebase\n\nIt's rare for companies to talk about backend changes.. but this one is super important\n\nWe kept having issues where our customers' tasks would randomly die. We had no idea what was going on\n\n1. We did running profiles of our"
---
<p>We fixed a memory leak in our codebase<br><br>It's rare for companies to talk about backend changes.. but this one is super important<br><br>We kept having issues where our customers' tasks would randomly die. We had no idea what was going on<br><br>1. We did running profiles of our EC2 instances and noticed that memory was spiking right before a worker would die... how interesting<br>2. We also knew one small quirk about our product: we would save all screenshots / llm calls / downloaded files to the /tmp/ directory as a users' task ran<br>3. We checked some AWS documentation and found out... that all data inside the /tmp directory is automatically loaded into memory (<a href="https://www.reddit.com/r/linuxquestions/comments/xowiye/a_stupid_question_but_is_tmp_actually_located_on/">https://www.reddit.com/r/linuxquestions/comments/xowiye/a_stupid_question_but_is_tmp_actually_located_on/</a>)<br><br>So our servers would write these very useful artifacts until we ran out of memory and blew up<br><br>We quickly started writing to another directory and our memory issues vanished.<br><br>Fortunately for our users, this means we can re-enable "fast" workflow runs! You should see your workflows starting instantly in the UI again<br></p>
