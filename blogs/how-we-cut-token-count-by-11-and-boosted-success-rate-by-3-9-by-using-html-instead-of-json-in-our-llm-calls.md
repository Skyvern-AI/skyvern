---
title: "How we cut token count by 11% and boosted success rate by 3.9% by using HTML instead of JSON in our LLM calls"
description: null
excerpt: "TL;DR\n\nWe started using HTML instead of JSON to represent possible actions that Skyvern could take and reduced our cost by 11.8% and increased our success rate by 3.9%\n\n\nWhat’s Skyvern?\n\nSkyvern is an open source AI agent that helps companies automate browser-based workflows with AI. You can use Skyvern to automate actions on any website with natural language: simple objective-based problems. Examples include going to www.geico.com and prompting it with “generate an auto insurance quote”.\n\nGithu"
slug: "how-we-cut-token-count-by-11-and-boosted-success-rate-by-3-9-by-using-html-instead-of-json-in-our-llm-calls"
publicationState: "published"
publishedAt: "2024-08-28T12:00:17.000Z"
updatedAt: "2024-08-28T12:11:28.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/ad53a3d334c16bbef67c288741053e93e4463d8d71872e571ed5a802b134e6bc-bvz9c-lkvrbj4bmuk-t7j.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
ogDescription: "TL;DR\n\nWe started using HTML instead of JSON to represent possible actions that Skyvern could take and reduced our cost by 11.8% and increased our success rate by 3.9%\n\n\nWhat’s Skyvern?\n\nSkyvern is an open source AI agent that helps companies automate browser-based workflows with"
---
<h2 id="tldr">TL;DR</h2>



We started using HTML instead of JSON to represent possible actions that Skyvern could take and reduced our cost by 11.8% and increased our success rate by 3.9%



<h2 id="what%E2%80%99s-skyvern">What’s Skyvern?</h2>





<p>Skyvern is an open source AI agent that helps companies automate browser-based workflows with AI. You can use Skyvern to automate actions on any website with natural language: simple objective-based problems. Examples include going to <a href="http://www.geico.com">www.geico.com</a> and prompting it with “generate an auto insurance quote”.</p>





<p>Github link here: <a href="https://github.com/Skyvern-AI/Skyvern">https://github.com/Skyvern-AI/Skyvern</a></p>





<p>Try it out for yourself here: <a href="https://app.skyvern.com/">https://app.skyvern.com/</a></p>





<p>Book a demo here: <a href="https://meetings.hubspot.com/suchintan">https://meetings.hubspot.com/suchintan</a></p>





<h2 id="what%E2%80%99s-the-problem">What’s the problem?</h2>





<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/d3ddee8df1e9c68ec01032b03ac1ce193e85364ef703880197946fc41ff5b166-blog-posts-image.png" class="kg-image" alt="" loading="lazy" width="549" height="407"><figcaption><span style="white-space: pre-wrap;">Image showing what we're improving in Skyvern</span></figcaption></figure>



Skyvern identifies and annotates elements on the screen and generates metadata about the interactable elements. It sends both the an annotated screenshot + metadata about the interactable elements to a multi-modal LLM (such as GPT-4O or Claude 3.5 Sonnet) to decide what actions to take to accomplish a users’ goal.



<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/bd678cdfb2b7916ebf539ba011903400d345c4b68abaf40de81db81256a4dd8d-blog-posts-image-1.png" class="kg-image" alt="" loading="lazy" width="557" height="475"><figcaption><span style="white-space: pre-wrap;">Image showing Skyvern in action</span></figcaption></figure>



Within that operation, a substantial portion of input tokens (our biggest expense) is consumed by the context associated with interactable elements

Specifically, Skyvern uses the annotated screenshot + a textual representation of the interactable elements to ground a LLM’s responses to reduce hallucinations (and therefore, increase accuracy) when interacting with random websites.

For the above screenshot, the grounded element tree includes elements that look like this:



<pre><code class="language-jsx">  {
    "id": "Pwir", 
    "interactable": true, 
    "tagName": "input",
    "attributes": {
      "name": "Id_GiveLastName_21593",
      "type": "text",
      "maxlength": "20",
      "aria-label": "Last Name"
    },
    "context": "Last Name"
  },. 
</code></pre>



Given that token counts are highly coupled with operational costs, we wanted to explore alternative data representation methods.

We talked to some other founders in the space, and did some research and found that others’ had success using markdown & HTML to compress their prompts by over 15% over using JSON data \[<a href="https://community.openai.com/t/markdown-is-15-more-token-efficient-than-json/841742" rel="noreferrer">1</a>\]. Markdown wouldn’t be useful to us because we want to encode tag-related information (ie `input`) inside the information passed to the LLM.



<h2 id="experimentation">Experimentation</h2>





<h3 id="step-0-do-basic-spot-checks">Step 0: Do basic spot checks</h3>



Before digging too deep, we wanted to just do a spot check to assert that we expect data represented as HTML takes fewer tokens than JSON. \[[OpenAI tokenizer](https://platform.openai.com/tokenizer)\]

**Sample Prompt + Element Tree (HTML):**



<pre><code class="language-html">// number of tokens: 31 
&lt;input id="Pwir" name="Id_GiveLastName_21593" type="text" maxlength="20" aria-label="Last Name"&gt;
</code></pre>



**Sample Prompt + Element Tree (JSON):**



<pre><code class="language-json">// number of tokens: 70 
{
    "id": "Pwir", 
    "interactable": true, 
    "tagName": "input",
    "attributes": {
      "name": "Id_GiveLastName_21593",
      "type": "text",
      "maxlength": "20",
      "aria-label": "Last Name"
    }
}
</code></pre>



Expanding this to a set of past Skyvern tasks, we can see that representing our data as HTML will reduce our result in approximately 20-27% Savings



<figure class="kg-card kg-image-card kg-card-hascaption"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/65e530fde4e2ec823084231ab3af1d84b749637bcc0d8b510f26af34a42832d7-blog-posts-image-2.png" class="kg-image" alt="" loading="lazy" width="646" height="565" srcset="https://dcbllm8dvghjo.cloudfront.net/media/blog/2e86834bcd41070cac28a081aa4b21616cbbf20440276ee2f513655d7753af69-blog-posts-image-2.png 600w, https://dcbllm8dvghjo.cloudfront.net/media/blog/65e530fde4e2ec823084231ab3af1d84b749637bcc0d8b510f26af34a42832d7-blog-posts-image-2.png 646w"><figcaption><span style="white-space: pre-wrap;">Box plot showing the impact of cost reduction across our tasks</span></figcaption></figure>





<h3 id="step-1-test-in-production">Step 1: Test in production</h3>



The primary goal of this experiment was to reduce operational costs without compromising the performance of Skyvern. To achieve this, we conducted an A/B test in production using this new set-up

For the A/B test, we divided tasks evenly between the original JSON representation and the new HTML representation. We tracked the success and failure rates of each approach on customer tasks, which is how our users derive value from Skyvern.



<h3 id="step-2-look-at-the-test-results"><strong>Step 2: Look at the t</strong>est Results</h3>



We ran the test on over ~1,100 tasks within Skyvern. Here’s the final breakdown:



<!--kg-card-begin: html-->
<table>
<thead>
<tr>
<th>Experiment</th>
<th>Status</th>
<th>Number of tasks</th>
<th>Net cost (p50)</th>
<th>Success rate</th>
</tr>
</thead>
<tbody>
<tr>
<td>HTML</td>
<td>success</td>
<td>332</td>
<td>1.08</td>
<td>63.8%</td>
</tr>
<tr>
<td></td>
<td>failed</td>
<td>188</td>
<td>2.82</td>
<td></td>
</tr>
<tr>
<td>JSON</td>
<td>success</td>
<td>391</td>
<td>1.22</td>
<td>59.9%</td>
</tr>
<tr>
<td></td>
<td>failed</td>
<td>261</td>
<td>3.13</td>
<td></td>
</tr>
</tbody>
</table>
<!--kg-card-end: html-->



-   <strong>Success Rate Impact:</strong>
    -   Overall, a <strong>3.9% improvement</strong> in success rate
    -   JSON representation: 59.9% success rate
    -   HTML representation: 63.8% success rate
-   <strong>Cost Impact:</strong>
    -   Overall, a <strong>11.4% reduction</strong> in net cost
    -   JSON representation: 1.22 average cost per task
    -   HTML representation: 1.08 average cost per task



<h2 id="counterintuitive-learnings">Counterintuitive learnings</h2>



We accomplished the goal we set out to do. **We reduced our operating costs by 11.4%.** YAY!

But.. something counter-intuitive also happened: we also improved our success rate. Why?

Our working hypothesis is that by cutting down the total context we’re sending to an LLM, we’ve reduced the rate of hallucinations that long context windows can provide \[[paper](https://arxiv.org/html/2402.11550v2)\]
