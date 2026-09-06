---
title: "Analytics and attribution for an open source project a.k.a story of how we tracked an unexpected burst of traffic"
description: null
excerpt: "TL;DR: We had a spike of traffic Monday (May 6th) morning, and we looked at our metrics to find out we got featured in a french newsletter and a viral tweet\n\n\nMonday Morning: Random burst of traffic\n\nLast Monday morning (May 6th), we woke up to a large burst of traffic to skyvern.com, an influx of Github stars (+1200) in our open source repo, and a burst of new users in our discord (+70) — which led us to scratch our heads.. where did this traffic come from? We hadn’t done any launches, no publi"
slug: "analytics-and-attribution-for-an-open-source-project-a-k-a-story-of-how-we-tracked-an-unexpected-burst-of-traffic"
publicationState: "published"
publishedAt: "2024-05-13T20:12:26.000Z"
updatedAt: "2024-05-13T20:52:38.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/dea99edf138ff260b51abaca7ba8525d52b783eed00f31a6e54aed0c1746ed7c-screenshot-2024-05-12-at-00-24-52.png"
featureImageAlt: null
featureImageCaption: "<span style=\"white-space: pre-wrap;\">Skyvern hitting 4.7K Stars in a random burst of traffic</span>"
sendNewsletter: false
migratedFromGhost: true
ogDescription: "TL;DR: We had a spike of traffic Monday (May 6th) morning, and we looked at our metrics to find out we got featured in a french newsletter and a viral tweet\n\n\nMonday Morning: Random burst of traffic\n\nLast Monday morning (May 6th), we woke up to a large burst"
---
**TL;DR:** We had a spike of traffic Monday (May 6th) morning, and we looked at our metrics to find out we got featured in [a french newsletter](https://korben.info/skyvern-automatisation-web-ia-vision-ordinateur.html) and <a href="https://twitter.com/tuturetom/status/1787296091475780054" rel="noreferrer">a viral tweet</a>



<h1 id="monday-morning-random-burst-of-traffic">Monday Morning: Random burst of traffic</h1>



Last Monday morning (May 6th), we woke up to a large burst of traffic to [skyvern.com](https://skyvern.com/), an influx of Github stars (+1200) in [our open source repo](https://github.com/Skyvern-AI/Skyvern), and a burst of [new users in our discord](https://discord.gg/fG2XXEuQX3) (+70) — which led us to scratch our heads.. where did this traffic come from? We hadn’t done any launches, no publicity, nothing.



<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/dea99edf138ff260b51abaca7ba8525d52b783eed00f31a6e54aed0c1746ed7c-screenshot-2024-05-12-at-00-24-52.png" class="kg-image" alt="Skyvern hitting 4.7K Github stars after a burst of traffic" loading="lazy" width="880" height="580" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/3d7892bac89a10f1a75a8cf91bf481939066201ad008a371e9fc21f4dbd0cf68-screenshot-2024-05-12-at-00-24-52.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/dea99edf138ff260b51abaca7ba8525d52b783eed00f31a6e54aed0c1746ed7c-screenshot-2024-05-12-at-00-24-52.png 880w" sizes="(min-width: 720px) 720px"><figcaption><span style="white-space: pre-wrap;">Skyvern hitting 4.7K Github stars</span></figcaption></figure>



Fortunately, we were cautious founders and had set up some analytics ahead of our first launch. It was time to roll up the sleeves and see how much information we can glean from these sources.



<h1 id="investigating-the-traffic">Investigating the traffic</h1>



Ahead of our initial Hackernews launch, we had set up two major pieces of analytics:

1.  Landing page attribution (via PostHog)
2.  [Open source installation and usage metrics](https://github.com/Skyvern-AI/Skyvern?tab=readme-ov-file#telemetry) (via PostHog)

This gave us a place to get started: we can look at this information to see what the source of this new traffic was



<h2 id="looking-at-landing-page-traffic">Looking at landing page traffic</h2>



We pulled up our Posthog Landing pages dashboard and navigated to the “Referring Domains” report and something new immediately stood out: what’s [korben.info](http://korben.info)?



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/ff9a809556333de73142a4a2d38118eee2892aff8fd7104e9de74a623ab6646e-untitled-19.png" class="kg-image" alt="Posthog dashboard showing referring domains" loading="lazy" width="800" height="394" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/b05b6fa1b7eec28de42ce76cb1f39b3adc989872bb2e8a71aa44b955a6f8f282-untitled-19.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/ff9a809556333de73142a4a2d38118eee2892aff8fd7104e9de74a623ab6646e-untitled-19.png 800w" sizes="(min-width: 720px) 720px"></figure>





<p>A quick google search for <a href="http://korben.info">korben.info</a> immediately reveals the source of information: <a href="https://korben.info/skyvern-automatisation-web-ia-vision-ordinateur.html">https://korben.info/skyvern-automatisation-web-ia-vision-ordinateur.html</a></p>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/be1e1f28de0437cfffe54595b747e9ad4c1a49e4b18e39b42cbe4071773cc22d-untitled-20.png" class="kg-image" alt="Google Search results showing korben.info featuring Skyvern" loading="lazy" width="897" height="280" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/ba07ca05a1d76bd41ae121f43dc6fa605d9ce7fb3c654f1ba2f1221e8a396770-untitled-20.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/be1e1f28de0437cfffe54595b747e9ad4c1a49e4b18e39b42cbe4071773cc22d-untitled-20.png 897w" sizes="(min-width: 720px) 720px"></figure>



Amazing! We’ve gotten to the bottom of it. All of our traffic came from [korben.info](http://korben.info).. right? How can we be sure?



<h2 id="looking-at-github-traffic-reports">Looking at Github traffic reports</h2>



To our surprise, Github also offers metrics for repo owners. They offer interesting metrics like:

1.  Git clones (ie number of unique users that have cloned Skyvern)
2.  Visitors (unique visitors to the github repo)
3.  Popular content (which files in the repo are the most popular?)
4.  Referring sites (sources of the traffic)

Looking a bit deeper into the Referring sites metric, we had one source stand out: [t.co](http://t.co)



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/2d1b454a5ba5696e94504bdaccc6adb4f45e7e7b92d22ec4f4fee3a31388d015-untitled-21.png" class="kg-image" alt="Github open source repo's referring sites" loading="lazy" width="462" height="421"></figure>





<p>We had a large spike of users coming from Twitter? What tweet could be the source of all this traffic? A quick search for Skyvern revealed the source: <a href="https://twitter.com/tuturetom/status/1787296091475780054">https://twitter.com/tuturetom/status/1787296091475780054</a></p>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/fd37265555a5178e84663e51b9cfce80cb068dd4364b9d417d192edad45d4cae-untitled-22.png" class="kg-image" alt="Tweet featuring Skyvern" loading="lazy" width="606" height="588" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/d47b4474d4df8bf12762ae215f6f8f19ebb5f3d419a5e51eea064650aeb2a7aa-untitled-22.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/fd37265555a5178e84663e51b9cfce80cb068dd4364b9d417d192edad45d4cae-untitled-22.png 606w"></figure>





<h1 id="finally-mystery-solved">Finally, mystery solved!</h1>



We got to the bottom of this burst of traffic! Two sources ([korbin.info](https://korben.info/skyvern-automatisation-web-ia-vision-ordinateur.html) & <a href="https://twitter.com/tuturetom/status/1787296091475780054" rel="noreferrer">tweet from @tuturetom</a>) featuring Skyvern both at the same time — how exciting!

But.. this is a good opportunity to do an audit of the data we collect about our visitors to help us (1) understand which direction we should double down on in our business, and (2) what friction our potential customers are experiencing as they go sample Skyvern



<h1 id="what-other-data-do-we-collect">What other data do we collect?</h1>



We use PostHog as our product analytics platform, tracking the two surfaces random people on the internet have to learn about our product:

1.  [Landing page](https://skyvern.com/)
2.  [Open Source repository](https://github.com/Skyvern-AI/Skyvern)



<h2 id="landing-page-analytics">Landing Page Analytics</h2>



PostHog (unlike GA-4) comes pre-packed with a lot of useful data collection about website visitors. It features metrics such as:

1.  Unique sessions on our landing page (over time)
2.  Average session duration
3.  Most popular destinations on our website
4.  Referring domains (where did this user come from?)
5.  Pages per session (How many pages do people visit before leaving the website)
6.  New & Returning users
7.  User breakdown by country, browser, device

Not much to say here — our landing page is [very very simple](https://skyvern.com/), so the pre-packaged metrics are good enough. We will likely publish a blog-post after we revamp our landing page, expanding some of these metrics and diving into user segments to figure out how subpages and detailed use-case descriptions on our landing page impact our conversion rate.



<h2 id="open-source-analytics">Open Source Analytics</h2>



We set up custom analytics to track the following in our open source repository:

1.  [Setup script completed](https://github.com/Skyvern-AI/skyvern/blob/main/setup.sh#L275) — Track how many people set Skyvern up
2.  [Server](https://github.com/Skyvern-AI/skyvern/blob/20a86590dd5c7430c6bb381b8b93e3818f116eb4/skyvern/forge/__main__.py#L13) and [UI “runs”](https://github.com/Skyvern-AI/skyvern/blob/20a86590dd5c7430c6bb381b8b93e3818f116eb4/streamlit_app/visualizer/streamlit.py#L20) — Track how many people are running Skyvern (post-setup)
3.  [Tasks Created](https://github.com/Skyvern-AI/skyvern/blob/20a86590dd5c7430c6bb381b8b93e3818f116eb4/skyvern/forge/agent.py#L798) [\+ Completed + Failed](https://github.com/Skyvern-AI/skyvern/blob/20a86590dd5c7430c6bb381b8b93e3818f116eb4/skyvern/forge/agent.py#L798) (unique and non-unique users)
    1.  Our users create tasks to instruct Skyvern to execute something in the browser. This is the most important metric to capture open source usage, and we keep track of how many tasks get created, how many fail, and how many are successful



<h3 id="things-we-don%E2%80%99t-track-but-we-definitely-should">Things we don’t track but we definitely should</h3>



We currently don’t track when exceptions / stack traces happen when someone is running Skyvern:

1.  Windows users have reported issues running Skyvern with Poetry. We would have been able to identify this sooner had we built exception tracking.
2.  Foreign users have seen issues running Skyvern if there is an issue with Posthog (ie if it’s blocked for some reason)



<h3 id="things-we-don%E2%80%99t-track-on-purpose">Things we don’t track on purpose</h3>



1.  We don’t track users’ emails or PII without their consent. We have access to their email if they agree to share it with us, but we felt it was an [invasion of privacy to grab it without their consent](https://news.ycombinator.com/item?id=32242374)
2.  Non-metadata task information such as instruction and task payloads. Skyvern users call Skyvern with a url, an instruction, and a payload. The payload contains information required for Skyvern to execute a task. Since payload and the instruction may contain sensitive information such as PII we only collect the URL and nothing else. This is a conscious decision that we made to preserve the privacy of our open source users.
