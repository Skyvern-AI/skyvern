---
title: "Skyvern MCP Server: Let AI Agents Control Your Browser (Updated June 2026)"
description: "Skyvern MCP Server lets Claude, Cursor, and Windsurf control your browser. Automate job applications, forms, and downloads through AI agents in June 2026 (Updated)."
excerpt: "We're building Skyvern, an open-source Agentic Process Automation (APA) platform. It uses computer vision and AI reasoning to read web pages visually, work through multi-step authentication flows, and complete browser-side tasks without brittle selectors or code that breaks the moment a site changes. Browser automation is the execution layer; the APA platform handles the broader job of autonomous, multi-step operation across portals and workflows that have no direct API to call.\n\nOver the weeken"
slug: "skyvern-mcp-server-let-agents-control-your-browser"
publicationState: "published"
publishedAt: "2025-04-03T19:24:01.000Z"
updatedAt: "2026-06-19T23:05:27.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/30788ec151072f8d1f009e3e612237c275fa91c3bc31a393ae6014e6a6b3ddc6-uzvom-o3zq6095ipqvue1.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Skyvern MCP: Let Agents Control Browsers Updated June 2026"
ogTitle: "Skyvern MCP: Let Agents Control Browsers Updated June 2026"
---
We're building Skyvern, an open-source Agentic Process Automation (APA) platform. It uses computer vision and AI reasoning to read web pages visually, work through multi-step authentication flows, and complete browser-side tasks without brittle selectors or code that breaks the moment a site changes. Browser automation is the execution layer; the APA platform handles the broader job of autonomous, multi-step operation across portals and workflows that have no direct API to call.

Over the weekend we built an MCP integration on top of it. The result: Claude, Cursor, and Windsurf can now call Skyvern as a native tool, describe a goal, and get back structured results after Skyvern works through the browser side. The code is open source: <a href="https://github.com/Skyvern-AI/skyvern/tree/main/integrations/mcp?ref=skyvern.com" rel="dofollow">Skyvern MCP on GitHub</a>



<h2 id="tldr">TLDR</h2>



-   Skyvern is an Agentic Process Automation platform. Browser automation is the execution layer. The MCP server exposes that layer to AI coding assistants.
-   Claude, Cursor, and Windsurf can now call Skyvern as a native tool — describe a goal, get back structured results after Skyvern works through the browser side.
-   Three working capabilities: Windsurf takes over a live Chrome session, Cursor handles forms and file downloads, Claude pulls live data from any public page.
-   Phone and SMS-based 2FA, multi-tab workflows, and Google Drive's native file picker are not yet supported.
-   The MCP server is open source. Cloud pricing is $0.05 per step. The self-hosted version is free.



<h2 id="what-the-skyvern-mcp-server-does">What the Skyvern MCP Server Does</h2>



The MCP server exposes Skyvern's browser automation layer as a native tool for any MCP-compatible AI assistant. Connect the assistant to Skyvern, describe a goal in plain language, and Skyvern handles the browser side. No selectors to write, no per-site integration code to maintain, and no code to patch when a portal changes its layout.

Three capabilities are working today.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/ccda8162203458239d64b61968fd4500abc9d07db1c6c8a3e11e1d57f81966a3-a55ntc4dimkgszfisspxi.png" class="kg-image" alt="" loading="lazy"></figure>





<h3 id="connect-windsurf-to-a-live-chrome-session">Connect Windsurf to a live Chrome session</h3>



Running Skyvern in "local" mode lets Windsurf take over your active Chrome window. The agent reads the current page state visually, identifies what's on screen by appearance and context, and acts on it without any hardcoded selectors. Point it at a portal you're already logged into and it picks up from there.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/4294e06eb50a3aa4bc5882449a90d84d2934ef0195f994c6b75e32c255a6be2b-ucvejyhquulgflc51uxwq.gif" class="kg-image" alt="" loading="lazy"></figure>





<h3 id="let-cursor-handle-forms-applications-and-file-downloads">Let Cursor handle forms, applications, and file downloads</h3>



Cursor can fill out contact forms, submit job applications, log in to portals, and download files on your behalf. Describe the goal in plain English and Skyvern works through the steps: reading each page visually, filling in fields, working through any authentication prompts, and returning the result.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/3c59ab96c7806ff809ed5b8c26f6166b89ed90461fb4218f7f601a0513621cb6-wekbjd-itarvzuc8fkcf5.gif" class="kg-image" alt="" loading="lazy"></figure>





<h3 id="let-claude-pull-live-information-from-any-web-page">Let Claude pull live information from any web page</h3>



Claude can open documentation sites, Stack Overflow, Hacker News, or any public page and return structured data from what it finds. Instead of relying on cached or training-data knowledge, the agent reads the live page state and returns current results. Ask it for the top posts on Hacker News right now and it goes and gets them.



<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/f92b1bee8e801d4b1c34c90044492fb7ae298df0f8fe8b4b0ae0f1f1911c5922-veklmrjcbs05yjuqannrh.gif" class="kg-image" alt="" loading="lazy"></figure>



We built this over a weekend to scratch an itch, and the use cases kept growing as we played with it. The most interesting pattern: AI coding assistants already know how to plan and reason; they just needed a browser execution layer to act on that reasoning. Skyvern provides that layer, connecting the goal-setting side of agents to the portal-heavy, credential-guarded work that still has no API to call.

That class of task is what Agentic Process Automation platforms are built for. Give an agent a goal; Skyvern handles the browser-side execution end to end. Whether that's booking appointments, downloading electricity statements, or pulling freight shipment information from a carrier portal, the APA platform takes it from goal to structured output.



<h2 id="code-example-running-an-authenticated-portal-task-via-the-python-sdk">Code Example: Running an Authenticated Portal Task via the Python SDK</h2>



The MCP server is the conversational interface, but the same browser execution layer is available directly through the Skyvern Python SDK. Here is what a production task looks like: log in to a portal, work through 2FA, extract structured data, and return the result.



<pre><code class="language-python">from skyvern import Skyvern
import asyncio

# Initialize with your Skyvern API key
skyvern = Skyvern(api_key="YOUR_API_KEY")

async def main():
    task = await skyvern.run_task(
        url="https://app.yourportal.com/invoices",

        # Describe the goal in plain language — no selectors to write
        prompt="Log in and download the most recent invoice.",

        # Reference credentials stored in the Skyvern vault — never passed to the LLM
        credential_id="cred_your_portal_credentials",

        # Routes TOTP codes to handle authenticator-app 2FA automatically
        totp_identifier="portal-totp@yourcompany.com",

        # Define the shape of structured output you want back
        data_extraction_schema={
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
                "amount":         {"type": "string"},
                "due_date":       {"type": "string"}
            }
        },

        # Notify your server when the run finishes
        webhook_url="https://your-server.com/skyvern-webhook",

        # Block until complete — useful for scripts that need the result inline
        wait_for_completion=True,
    )

    print(task.status)           # "completed" or "failed"
    print(task.output)           # structured JSON matching the schema above
    print(task.downloaded_files) # any files retrieved during the run

asyncio.run(main())</code></pre>



Skyvern reads the portal visually at runtime, so when the portal changes its layout nothing in the code breaks. There are no selectors to patch. The same pattern works whether the entry point is the Python SDK, the MCP server, or a workflow triggered through the API.



<h2 id="limitations">Limitations</h2>



The MCP integration works well for the use cases above, though a few constraints are worth knowing before you build around it.

-   <strong>Phone and SMS-based 2FA is not supported.</strong> Portals that require a phone number or SMS code for authentication will block automated workflows. TOTP-based authenticator apps and email-based OTP with forwarding integration are both supported.
-   <strong>Anti-bot detection varies by site.</strong> Skyvern works through most authentication challenges, but high-security portals with aggressive bot detection can block runs. Running a proof-of-concept against your specific target sites before committing to production is good practice.
-   <strong>Multi-tab workflows are not yet supported.</strong> Tasks that require keeping multiple browser tabs open simultaneously need to be restructured as sequential steps.
-   <strong>Google Drive's native file picker does not work with Skyvern's file handling.</strong> Files stored in AWS S3 cannot be uploaded through the Drive picker. Drag-and-drop upload modals and publicly accessible file URLs work correctly.
-   <strong>Complex branching workflows are better built elsewhere.</strong> For simple, well-scoped browser tasks the MCP integration works cleanly. Multi-step workflows with conditional logic and exception handling are better constructed through Skyvern's visual workflow builder or Python SDK.



<h2 id="final-thoughts">Final Thoughts</h2>



The MCP server is open source and ready to connect to Claude, Cursor, or Windsurf today. If you're working on production workflows at scale, that's where Skyvern's Agentic Process Automation platform goes further: multi-step operation across portals, credential management, audit trails, and exception handling built in.

<a href="https://meetings.hubspot.com/skyvern/demo?uuid=7c83865f-1a92-4c44-9e52-1ba0dbc04f7a" rel="dofollow">Book a demo to see what Skyvern can automate for your team.</a>



<h2 id="faq">FAQ</h2>





<h3 id="what-is-the-skyvern-mcp-server">What is the Skyvern MCP Server?</h3>



The Skyvern MCP Server connects MCP-compatible AI assistants — Claude, Cursor, and Windsurf — to a live browser session through Skyvern's Agentic Process Automation platform. Describe a goal in plain language and Skyvern handles the browser side: authentication, form filling, file downloads, and data extraction from live pages. No selectors, no per-site integration code.



<h3 id="how-do-i-connect-claude-to-the-skyvern-mcp-server">How do I connect Claude to the Skyvern MCP Server?</h3>



Run this command with your Skyvern API key:



<pre><code class="language-bash">claude mcp add-json skyvern '{"type":"http","url":"https://api.skyvern.com/mcp/","headers":{"x-api-key":"YOUR_SKYVERN_API_KEY"}}' --scope user</code></pre>



Once connected, Claude can call browser tasks directly from a conversation using Skyvern as the execution layer.



<h3 id="does-the-mcp-server-work-with-cursor-and-windsurf">Does the MCP Server work with Cursor and Windsurf?</h3>



Yes. Both support MCP integrations natively. In local mode, Skyvern takes over the active Chrome window, reads the current page state visually, and acts on it in response to the agent's instructions — no hardcoded selectors needed.



<h3 id="what-kinds-of-tasks-can-the-mcp-server-handle">What kinds of tasks can the MCP Server handle?</h3>



Form submissions, job applications, portal logins, file downloads, and live data lookups from public web pages. For multi-step APA workflows spanning multiple portals or requiring credential management at scale, Skyvern's full workflow platform goes further.



<h3 id="what-authentication-methods-are-supported">What authentication methods are supported?</h3>



Skyvern handles password-based logins, TOTP-based authenticator apps (six-digit time codes), and email-based OTP via forwarding integration. Phone and SMS-based 2FA is not currently supported.



<h3 id="is-the-skyvern-mcp-server-available-to-all-users">Is the Skyvern MCP Server available to all users?</h3>



Yes. The MCP server is available to all Skyvern customers regardless of plan tier. Skyvern's cloud platform charges $0.05 per step. The open-source version can be self-hosted at no cost.



<h3 id="how-is-the-skyvern-mcp-server-different-from-writing-playwright-or-selenium-scripts">How is the Skyvern MCP Server different from writing Playwright or Selenium scripts?</h3>



Traditional browser automation scripts rely on hardcoded selectors that break whenever a site changes its layout. Skyvern reads pages visually using computer vision, so it works without per-site code. Describe the goal; Skyvern figures out the steps.



<h3 id="what-is-agentic-process-automation-apa">What is Agentic Process Automation (APA)?</h3>



APA is a category of automation that gives AI agents a goal and lets them complete multi-step processes autonomously — across portals, authentication flows, and web-based systems that have no direct API. Browser automation is the execution layer; APA is the broader framework that handles orchestration, credential management, and structured output. Skyvern is built as an APA platform.
