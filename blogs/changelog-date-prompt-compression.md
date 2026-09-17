---
title: "Changelog - September - Prompting guide, SVG Handling, Persistent Sessions, Advanced 2FA"
description: null
excerpt: "New Prompting Guide\n\nWe just released a new prompting guide: https://docs.skyvern.com/getting-started/prompting-guide\n\nIt goes into detail talking about a few gotchas.\n\nFor example, did you know:\n\n 1. Skyvern works the most reliably pairing prompts with (goal, completion criteria) pairs?\n    1. It turns out that LLMs are very similar to humans in this regard – the more vague you are about when something is \"done\", the less likely they are to do it the way you expect\n 2. You can use keywords like"
slug: "changelog-date-prompt-compression"
publicationState: "published"
publishedAt: "2024-10-02T14:41:05.000Z"
updatedAt: "2024-10-02T14:41:04.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/957f4a9b45f88bf62cb110fc94671afaec0c66fa8e3526baa84be4c1593a2bdd-image.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "New Prompting Guide\n\nWe just released a new prompting guide: https://docs.skyvern.com/getting-started/prompting-guide\n\nIt goes into detail talking about a few gotchas.\n\nFor example, did you know:\n\n 1. Skyvern works the most reliably pairing prompts with (goal, completion criteria) pairs?\n    1. It turns out that"
---
<h1 id="new-prompting-guide">New Prompting Guide</h1>





<p>We just released a new prompting guide: <a href="https://docs.skyvern.com/getting-started/prompting-guide">https://docs.skyvern.com/getting-started/prompting-guide</a></p>



It goes into detail talking about a few gotchas.

For example, did you know:

1.  Skyvern works the most reliably pairing prompts with (goal, completion criteria) pairs?
    1.  It turns out that LLMs are very similar to humans in this regard – the more vague you are about when something is "done", the less likely they are to do it the way you expect
2.  You can use keywords like "complete" and "terminate" to let Skyvern know when it's finished successfully, or finished through failure. For example, you can say "terminate if no products are found", and Skyvern will terminate + provide you its reasoning
3.  You can add guardrails like "close cookie dialogs" or "don't click the big red button" into your prompts to help it dynamically take care of these things



<p>Read more here: <a href="https://docs.skyvern.com/getting-started/prompting-guide">https://docs.skyvern.com/getting-started/prompting-guide</a></p>





<h1 id="prompt-compressionsvg">Prompt Compression -&gt; SVG</h1>



SVG Handling is here!!<br><br>Turns out, closing random popup dialogs is an impossible task for the old generation of RPA tools<br><br>If you're using tools like PowerAutomate to automate purchasing workflows, you might add a step to close a popup dialog<br><br>Suddenly, they change the colour of the X and your power automate script breaks.. and now you need to do it over again<br><br>This is one of the most frequent questions I get on calls. "Can Skyvern handle random popup dialogs?"<br><br>We've always been able to handle it, but we just released a new SVG-summarization technique that converts SVG Renderings of icons (ie X buttons) into summaries. <br><br>What does it look like in practice?

&lt;svg alt="magnifying glass, representing search functionality"&gt;



<figure class="kg-card kg-video-card kg-width-regular" data-kg-thumbnail="https://blog.skyvern.com/content/media/2024/08/livestream-of-svg-handling_thumb.jpg" data-kg-custom-thumbnail=""><div class="kg-video-container"><video src="https://dcbllm8dvghjo.cloudfront.net/media/blog/video/32f06a13c72a3a1430634c774c3760118c189e717e000b389b5b6992c8f87e9d-livestream-of-svg-handling.mp4" poster="https://dcbllm8dvghjo.cloudfront.net/media/blog/435ef5ec680574f354ab07494223c26317d0fffd1e084808c01ee692953ae9bc-spacer.png" width="1178" height="720" playsinline="" preload="metadata" style="background: transparent url('https://dcbllm8dvghjo.cloudfront.net/media/blog/6fe07cc676442026117c9999ef181a9c736211026ce7ff64868cb8a8c633b7cd-livestream-of-svg-handling-thumb.jpg') 50% 50% / cover no-repeat;"></video><div class="kg-video-overlay"><button class="kg-video-large-play-icon" aria-label="Play video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M23.14 10.608 2.253.164A1.559 1.559 0 0 0 0 1.557v20.887a1.558 1.558 0 0 0 2.253 1.392L23.14 13.393a1.557 1.557 0 0 0 0-2.785Z"></path></svg></button></div><div class="kg-video-player-container"><div class="kg-video-player"><button class="kg-video-play-icon" aria-label="Play video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M23.14 10.608 2.253.164A1.559 1.559 0 0 0 0 1.557v20.887a1.558 1.558 0 0 0 2.253 1.392L23.14 13.393a1.557 1.557 0 0 0 0-2.785Z"></path></svg></button><button class="kg-video-pause-icon kg-video-hide" aria-label="Pause video"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="3" y="1" width="7" height="22" rx="1.5" ry="1.5"></rect><rect x="14" y="1" width="7" height="22" rx="1.5" ry="1.5"></rect></svg></button><span class="kg-video-current-time">0:00</span><div class="kg-video-time">
                            /<span class="kg-video-duration">0:14</span></div><input type="range" class="kg-video-seek-slider" max="100" value="0"><button class="kg-video-playback-rate" aria-label="Adjust playback speed">1×</button><button class="kg-video-unmute-icon" aria-label="Unmute"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M15.189 2.021a9.728 9.728 0 0 0-7.924 4.85.249.249 0 0 1-.221.133H5.25a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3h1.794a.249.249 0 0 1 .221.133 9.73 9.73 0 0 0 7.924 4.85h.06a1 1 0 0 0 1-1V3.02a1 1 0 0 0-1.06-.998Z"></path></svg></button><button class="kg-video-mute-icon kg-video-hide" aria-label="Mute"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M16.177 4.3a.248.248 0 0 0 .073-.176v-1.1a1 1 0 0 0-1.061-1 9.728 9.728 0 0 0-7.924 4.85.249.249 0 0 1-.221.133H5.25a3 3 0 0 0-3 3v2a3 3 0 0 0 3 3h.114a.251.251 0 0 0 .177-.073ZM23.707 1.706A1 1 0 0 0 22.293.292l-22 22a1 1 0 0 0 0 1.414l.009.009a1 1 0 0 0 1.405-.009l6.63-6.631A.251.251 0 0 1 8.515 17a.245.245 0 0 1 .177.075 10.081 10.081 0 0 0 6.5 2.92 1 1 0 0 0 1.061-1V9.266a.247.247 0 0 1 .073-.176Z"></path></svg></button><input type="range" class="kg-video-volume-slider" max="100" value="100"></div></div></div></figure>





<h1 id="persistent-sessions">Persistent Sessions</h1>



Skyvern now supports persistent sessions 🎉

Are you using Skyvern's beta workflows feature? Well now you can toggle on persistent sessions. Skyvern logs into a website once, and keeps the session state for future workflow runs saving you a login step!

This is a huge unlock for very frequent workflows as repeatedly logging in adds a lot of execution time to any given task

Now they're 10x faster and more efficient than before



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/506af42df2677871e6586fa3b863d227edad7ef2a8ab916c3c91f22e2b3e8c18-screenshot-2024-09-06-at-22-05-58.png" class="kg-image" alt="" loading="lazy" width="996" height="1660" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/d0519e59cfd7b75a2e26483456022d91fdbeee8159eebf5aa7e705375c266bf6-screenshot-2024-09-06-at-22-05-58.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/506af42df2677871e6586fa3b863d227edad7ef2a8ab916c3c91f22e2b3e8c18-screenshot-2024-09-06-at-22-05-58.png 996w" sizes="(min-width: 720px) 720px"></figure>





<h1 id="advanced-2fa-supportpush-codes-to-skyvern">Advanced 2FA Support - Push codes to Skyvern</h1>



Skyvern got a beefy upgrade today. You can now push 2FA (Two-factor authentication) codes to Skyvern, and Skyvern will use them to log-in in real time

2FA is becoming an increasingly popular method of making accounts more secure.. but it introduces a big problem: it makes automations much harder to run

Agent-based automations like Skyvern are no exception

So how do we solve this problem?

Skyvern now has a dedicated endpoint for receiving new 2FA codes. You can use third party tools like Zapier or Make.com to listen to e-mail (Gmail) or texts (Twilio), and publish them directly to Skyvern

Skyvern is intelligent enough to pause executing when faced with a 2FA code, and wait til it receives the code to continue logging in

This is a huge unlock for companies. Most old-school software slapped 2FA on as a measure of security, making it much harder to use or automate the system. I wish they spent that much time building a usable API instead!!



<p>Check out the documentation here: <a href="https://docs.skyvern.com/running-tasks/advanced-features#push-code-to-skyvern">https://docs.skyvern.com/running-tasks/advanced-features#push-code-to-skyvern</a></p>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/957f4a9b45f88bf62cb110fc94671afaec0c66fa8e3526baa84be4c1593a2bdd-image.png" class="kg-image" alt="" loading="lazy" width="606" height="412" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/89aaf4440e7e3b38cb456a96ef66b6fc023a234e284e4a091aa3618af15d691f-image.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/957f4a9b45f88bf62cb110fc94671afaec0c66fa8e3526baa84be4c1593a2bdd-image.png 606w"></figure>
