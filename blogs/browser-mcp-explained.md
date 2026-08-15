---
title: "Browser MCP in 2026: What It Is, How It Functions, and the Best Servers Available"
description: "Learn what Browser MCP is, how it works, and explore the top server options in June 2026. Complete guide to browser automation with AI agents."
excerpt: "Most browser automation scripts fail the moment a site changes its layout. A button moves, a class name updates, and your carefully crafted Selenium script stops working. Skyvern solves this by giving AI agents live browser access instead of hardcoded selectors. The agent sees what's actually on the screen, decides what to do next, and adapts when things change. That's the difference between brittle scripts that need constant maintenance and automation that keeps working. This guide covers what "
slug: "browser-mcp-explained"
publicationState: "published"
publishedAt: "2026-06-27T00:03:49.000Z"
updatedAt: "2026-06-27T00:03:48.000Z"
author: "suchintan"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/4b3df777142687260baf8d7d75e4b40466d08a729e7986768a026ead7edbc2ff-v1lfpyums-q5fhbylvvgv.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Browser MCP Guide: Top Options June 2026"
ogTitle: "Browser MCP Guide: Top Options June 2026"
---
Most browser automation scripts fail the moment a site changes its layout. A button moves, a class name updates, and your carefully crafted Selenium script stops working. <a href="https://skyvern.com/" rel="dofollow">Skyvern</a> solves this by giving AI agents live browser access instead of hardcoded selectors. The agent sees what's actually on the screen, decides what to do next, and adapts when things change. That's the difference between brittle scripts that need constant maintenance and automation that keeps working. This guide covers what browser MCP is, how it's structured, which server implementations are worth your time, and how to set one up with tools like Cursor and Claude Code.

**TLDR:**

-   Browser MCP lets AI agents control real browsers to interact with login-protected pages and dynamic content that APIs cannot reach.
-   It uses a client-server architecture where agents call standardized tools for clicking, typing, and inspecting pages instead of hardcoded selectors.
-   Script maintenance drops because agents adapt to layout changes instead of breaking when a button moves or a class name changes.
-   Popular servers include agentdeskai/browser-tools-mcp for Chrome, Puppeteer MCP, and Playwright MCP for multi-browser testing.
-   Skyvern uses computer vision to read pages visually, so workflows keep running when layouts change without manual fixes.



<h2 id="what-is-browser-mcp-and-why-it-matters-in-2026">What Is Browser MCP and Why It Matters in 2026</h2>



Browser MCP is a specification that lets AI agents control a real web browser during a task. Instead of guessing what a webpage contains from a text summary, an agent equipped with a <a href="https://www.skyvern.com/blog/browser-automation-mcp-servers-guide/" rel="dofollow">browser MCP server</a> can click buttons, fill forms, read dynamic content, and observe the results in real time.

The "MCP" stands for Model Context Protocol, an open standard that gives LLMs a structured way to call external tools. Browser MCP applies that protocol directly to browser control, so any compatible agent can open URLs, inspect the DOM, take screenshots, or run JavaScript without custom glue code for every project.

In 2026, this matters because most valuable workflows live behind login screens, multi-step forms, and JavaScript-generated pages that plain API calls cannot reach. <a href="https://www.digitalapplied.com/blog/mcp-adoption-statistics-2026-model-context-protocol" rel="nofollow">78% of enterprise AI teams use MCP</a>, making browser MCP a practical standard and no longer an experimental tool. Browser MCP bridges that gap, letting agents operate the web the way a person would.



<h2 id="how-browser-mcp-works-architecture-and-core-components">How Browser MCP Works: Architecture and Core Components</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/c4b7651fafb30b80d1daf206928f97c7a2ab4a1ac3dfe99b6958941fcbcefb2a-v9pkdy-jywnpyt-ekb-1o.png" class="kg-image" alt="" loading="lazy"></figure>



Browser MCP follows a client-server architecture where your AI agent or coding tool acts as the MCP client, and a locally running process acts as the MCP server. The server exposes a set of standardized tools that the client can call, each corresponding to a browser action like clicking, typing, scrolling, or taking a screenshot.

There are a number of components that work together here:

-   The MCP server process runs locally on your machine and manages one or more browser instances, either through a Chrome DevTools Protocol connection to an existing browser or by spinning up a headless instance programmatically.
-   The AI client sends tool-call requests over a local transport layer, typically stdio or HTTP with server-sent events, and receives structured responses containing page state, screenshots, or extracted data.
-   The browser bridge handles the low-level communication between the server and the browser, forwarding commands and returning DOM snapshots, console logs, or network activity depending on what the tool requested.

When a user asks Claude or Cursor to complete a web task, the LLM decides which browser tool to call, passes the necessary arguments, and interprets the result before deciding its next step. This loop continues until the task is complete or the agent hits an error it cannot resolve on its own.



<h2 id="browser-mcp-vs-traditional-browser-automation">Browser MCP vs Traditional Browser Automation</h2>



Traditional <a href="https://www.skyvern.com/blog/what-is-browser-automation/" rel="dofollow">browser automation</a> tools like Selenium, Playwright, and Puppeteer work by targeting specific DOM elements through selectors, XPaths, or CSS classes. When a website updates its layout, those selectors break, and your scripts stop working. Maintaining these scripts requires constant attention from engineers who understand both the codebase and the sites being automated.

Browser MCP takes a different approach. Instead of hardcoding element selectors, it exposes browser state and actions as structured tools that an AI agent can call dynamically. The agent reads what's actually on the screen and decides how to interact with it, making the automation far more resilient to UI changes.

There are a few ways this plays out in practice:

-   Script maintenance drops considerably because the agent adapts to layout changes instead of failing when a button moves or a class name changes.
-   Complex multi-step workflows become easier to express as natural instructions instead of procedural code with dozens of brittle intermediate steps.
-   Non-engineers can describe tasks in plain language, and the agent handles the execution details, lowering the barrier to building useful automations.

The tradeoff is that browser MCP setups introduce LLM inference into the loop, which adds latency and cost compared to direct script execution. For high-volume, repetitive tasks where the page structure never changes, traditional automation can still be faster and cheaper. But for tasks involving dynamic content, varied site structures, or workflows that require judgment calls, browser MCP handles cases that traditional scripts simply cannot.



<h2 id="key-features-of-browser-mcp-servers">Key Features of Browser MCP Servers</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8c400f13c1b25587574ac0ab4d29dee4ef39bbc00fd8602dde6c41cfea8dc46d-pqrdccx5ejmiwqlaksnzn.png" class="kg-image" alt="" loading="lazy"></figure>



Browser MCP servers expose a consistent set of capabilities that let AI agents interact with the web the way a human would, without requiring custom integration code for every new task.

There are a number of core capabilities that define what a browser MCP server actually does:

-   Page navigation and URL control, allowing agents to load pages, follow links, and manage browser history across multi-step workflows.
-   DOM inspection and element interaction, so agents can read page content, click buttons, fill out forms, and trigger JavaScript events based on what's visible on screen.
-   Screenshot capture and visual context, giving agents a pixel-level view of the page to handle sites where the underlying HTML alone doesn't tell the full story.
-   Console and network log access, letting agents inspect errors, monitor API calls, and debug page behavior in real time.
-   Tab and session management, so agents can open multiple windows, switch contexts, and maintain authenticated sessions across longer workflows.



<h3 id="how-these-features-work-together">How These Features Work Together</h3>



These capabilities become meaningful when combined. An agent submitting a form, for example, needs to read the page structure, identify the right fields, type into them, handle any validation errors that appear, and confirm the submission succeeded. A browser MCP server gives the agent all the hooks it needs to complete that sequence without brittle CSS selectors or hardcoded scripts.

The breadth of what's exposed also matters for reliability. Agents that can only click and type tend to fail when pages behave unexpectedly. Access to console logs and network responses gives the agent enough context to recover from errors instead of silently failing midway through a task.



<h2 id="top-browser-mcp-server-options">Top Browser MCP Server Options</h2>



There are several browser MCP server options worth knowing about, each suited to different workflows and tools. Here is a look at the most widely used ones.



<h3 id="agentdeskaibrowser-tools-mcp">agentdeskai/browser-tools-mcp</h3>



agentdeskai/browser-tools-mcp is a browser MCP server that bridges AI coding tools and Chrome through a browser extension paired with a local Node.js server. It is one of the most referenced options in the developer community. It gives tools like Cursor and Claude Code access to console logs, network requests, screenshots, and DOM snapshots, making it a strong fit for debugging and iterative development workflows.



<h3 id="puppeteer-mcp-server">Puppeteer MCP Server</h3>



Puppeteer MCP Server is a browser automation server built on top of Google's Puppeteer library. It gives LLMs programmatic control over a headless Chrome instance and suits teams that need reliable browser automation and are comfortable with a code-forward setup.



<h3 id="playwright-mcp-server">Playwright MCP Server</h3>



Microsoft's <a href="https://www.skyvern.com/blog/playwright-mcp-reviews-and-alternatives-2025/" rel="dofollow">Playwright MCP server</a> supports Chrome, Firefox, and WebKit in 2026. It is a strong pick for multi-browser testing environments where cross-browser consistency matters.



<h3 id="skyvern">Skyvern</h3>



Skyvern takes a different approach. Instead of relying on DOM selectors or hardcoded scripts, it uses computer vision and AI reasoning to interact with web pages the way a human would. This makes it far more resilient when page layouts change. Skyvern exposes an MCP-compatible server that works with Cursor, Claude Code, and other MCP clients, allowing agents to complete complex, multi-step browser tasks without brittle element targeting.



<h3 id="side-by-side-comparison-of-mcp-servers">Side-by-Side Comparison of MCP Servers</h3>





<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Server</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Primary Browser Support</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Automation Approach</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Best For</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Resilience to Layout Changes</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Skyvern</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Chrome via computer vision and AI reasoning</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Visual understanding of page elements instead of DOM selectors, adapts when layouts change</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Production workflows with authentication, dynamic sites, and multi-step tasks that need to work across layout updates</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>High - uses computer vision to identify elements visually, so changes to HTML structure or class names do not break workflows</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>agentdeskai/browser-tools-mcp</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Chrome via extension and local Node.js server</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>DOM inspection, console logs, network requests, and screenshots through browser extension</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Development and debugging workflows where you need live browser state visibility during iterative coding</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Low - relies on DOM structure and element selectors that break when sites update their HTML</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Puppeteer MCP Server</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Headless Chrome via Puppeteer library</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Programmatic browser control through DevTools Protocol with code-forward configuration</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Teams comfortable with code-based setup who need reliable headless automation for stable page structures</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Low - uses explicit element targeting that fails when selectors change during site updates</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Playwright MCP Server</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Chrome, Firefox, and WebKit cross-browser support</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Multi-browser testing with programmatic control across different browser engines</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>QA teams running cross-browser test suites where consistent behavior across browsers matters</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Low - selector-based automation breaks on layout changes across all supported browsers</p></td></tr></tbody></table>
<!--kg-card-end: html-->





<h2 id="setting-up-browser-mcp-with-claude-code">Setting Up Browser MCP with Claude Code</h2>



Claude Code supports two paths for adding a browser MCP server, depending on how much control you want over the setup.



<h3 id="project-level-vs-global-configuration">Project-level vs. global configuration</h3>



You can scope your MCP server to a single project by editing the `.mcp.json` file in your project root, or register it globally so every Claude Code session can access it. Most developers start with project-level scoping to keep configurations isolated.



<h3 id="installation-steps">Installation steps</h3>



-   Run `claude mcp add` in your terminal and follow the prompts to register your chosen browser MCP server by name and command path.
-   Confirm the server appears when you run `claude mcp list`, which shows all active MCP connections for your current scope.
-   Test the connection by asking Claude Code to open a URL or take a screenshot, which triggers the browser MCP tools directly from the chat interface.



<h2 id="installing-browser-mcp-in-cursor-ide">Installing Browser MCP in Cursor IDE</h2>



Cursor stores MCP server configurations in a JSON file at `~/.cursor/mcp.json`, making the setup process different from Claude Code's command-line registration flow. There's no `mcp add` command here. You edit the config file directly.



<h3 id="configuration-file-setup">Configuration file setup</h3>



Open or create `~/.cursor/mcp.json` and add your browser MCP server as an entry under the `mcpServers` key:



<pre><code class="language-json">{
  "mcpServers": {
    "browser-tools": {
      "command": "npx",
      "args": ["@agentdeskai/browser-tools-mcp"]
    }
  }
}
</code></pre>



For setting up Skyvern's cloud-based MCP server in Cursor IDE, use HTTP transport instead:



<pre><code class="language-json">{
  "mcpServers": {
    "skyvern": {
      "type": "http",
      "url": "https://api.skyvern.com/mcp/",
      "headers": {
        "x-api-key": "YOUR_SKYVERN_API_KEY"
      }
    }
  }
}
</code></pre>



Save the file and restart Cursor. The MCP tools appear in the agent panel once the server connects.



<h3 id="verifying-the-connection">Verifying the connection</h3>



Ask Cursor's AI to take a screenshot of a URL or inspect an element on a running page. If the browser tools respond, the setup worked. If you see a timeout or "tool not found" error, check that the server process is running and that the command path in your config resolves correctly on your system.

Cursor's agent mode is where browser MCP tools shine most. Describing a task like "check if the login form submits correctly on localhost:3000" lets the agent move through, interact, and report back without you writing a single test script.



<h2 id="browser-mcp-extensions-for-chrome-and-firefox">Browser MCP Extensions for Chrome and Firefox</h2>



Browser MCP extensions bring AI-assisted browser control directly into Chrome and Firefox, letting AI agents observe and interact with web pages without requiring a separate server process running in the background.

For Chrome users, the most common setup pairs a browser extension with a local MCP server. The extension injects scripts into active tabs, captures DOM snapshots, monitors network requests, and forwards that data to whatever AI client is connected. Chrome DevTools Protocol access gives these extensions low-level visibility into page state that surface-level scraping cannot match.

Firefox support follows a similar pattern, though the extension ecosystem is less mature. Most browser MCP projects list Chrome as the primary target, with Firefox support arriving later or maintained as a community contribution. If Firefox is your default browser, check the project's GitHub issues before committing to a setup.



<h3 id="what-to-look-for-in-a-browser-mcp-extension">What to look for in a browser MCP extension</h3>



There are a number of factors worth checking before installing any browser MCP extension:

-   <a href="https://www.skyvern.com/blog/browser-automation-security-best-practices/" rel="dofollow">Permissions requested at install time</a>, since broad host permissions can expose sensitive session data to the connected AI client
-   Whether the extension supports Manifest V3, as Chrome is phasing out Manifest V2 extensions
-   Active maintenance signals like recent commits and open issue responses on the project's GitHub repository
-   Compatibility with your AI client, whether that is Cursor, VS Code, or Claude Code, since not every extension ships with configs for all three



<h2 id="common-use-cases-for-browser-mcp">Common Use Cases for Browser MCP</h2>



Browser MCP slots into workflows where AI assistants keep hitting walls because they lack live browser access. There are a number of recurring scenarios where it earns its place:

-   <strong>Authenticated web scraping</strong>: log in, move behind a session, and extract structured data from portals that block unauthenticated requests.
-   <a href="https://www.skyvern.com/blog/selenium-alternatives-5-better-browser-automation-tools-in-2025/" rel="dofollow"><strong>Automated QA testing</strong></a>: ask your AI assistant to run through a checkout flow or login sequence on localhost before you push, with no test scripts to maintain.
-   <strong>Form automation</strong>: fill and submit repetitive forms across multiple sites using natural language instructions instead of per-site scripts.
-   <strong>OAuth and browser-based API flows</strong>: complete authorization redirects that REST clients cannot handle, then hand off the token to downstream logic.
-   <strong>Frontend debugging</strong>: give your AI access to console errors and network traces while it inspects a broken page, so you get a diagnosis alongside the fix.



<h2 id="troubleshooting-browser-mcp-setup-and-connection-issues">Troubleshooting Browser MCP Setup and Connection Issues</h2>



Most browser MCP failures fall into a handful of predictable categories. Here is how to diagnose and fix the common ones.



<h3 id="connection-refused-errors">Connection Refused Errors</h3>



Check that the server started correctly by running it manually in your terminal and watching for startup errors. If the process launches but connections still fail, confirm no other service is occupying the expected port. On most systems, `lsof -i :<port>` shows what is using a given port.



<h3 id="timeout-errors-on-long-browser-tasks">Timeout Errors on Long Browser Tasks</h3>



Browser MCP clients have default request timeouts that shorter tasks never hit. Multi-step workflows can exceed these limits easily, so raise them if your client supports it, or break the task into smaller chained steps.



<h3 id="extension-not-detected-by-the-mcp-server">Extension Not Detected by the MCP Server</h3>



The server needs to be running before the browser opens the tab. Starting them in the wrong order means the extension cannot create its WebSocket connection. Restart Chrome after confirming the server is up, then reload the active tab.



<h3 id="captcha-and-bot-detection-blocking-workflows">CAPTCHA and Bot Detection Blocking Workflows</h3>



If the server uses a headless instance or datacenter IP, sites with aggressive bot detection will block it. Switching to non-headless mode and routing traffic through a residential proxy resolves most cases.



<h3 id="mcp-server-process-crashes-mid-task">MCP Server Process Crashes Mid-Task</h3>



Crashes often trace back to an unhandled exception, memory pressure from many open tabs, or a missing dependency surfacing at runtime. Run the server with verbose logging turned on so you get a stack trace instead of a silent exit.



<h2 id="how-skyvern-provides-production-grade-browser-automation">How Skyvern Provides Production-Grade Browser Automation</h2>



Skyvern approaches browser automation differently from most MCP-based tools. Instead of relying on DOM selectors or fragile CSS paths, Skyvern uses computer vision and AI to read web pages the way a human would, identifying elements by what they look like on screen instead of how they're structured in the underlying code.

This matters because most browser MCP setups break when a website updates its layout. Skyvern, though, adapts visually, so workflows keep running without manual fixes.

There are a number of things that make Skyvern well-suited for production use:

-   Skyvern reads pages visually, so layout changes, A/B tests, and dynamic content don't break running workflows the way they do with selector-based tools.
-   <a href="https://www.skyvern.com/blog/how-skyvern-handles-authentication/" rel="dofollow">Authentication handling</a> is built in, covering OAuth flows, MFA prompts, and session management without requiring custom workarounds.
-   Workflows can run across multiple sites without site-specific configuration, since Skyvern reasons about what's on screen instead of following hardcoded instructions.
-   The explainable AI component explains every action taken, which matters for teams that need audit trails or want to debug unexpected behavior.
-   Skyvern runs as a managed cloud service, removing the infrastructure overhead that comes with self-hosted MCP server setups.

For teams that have outgrown simple browser MCP configurations and need automation that holds up under real-world conditions, skyvern.com offers a starting point for seeing what production-grade browser automation looks like in practice.



<h2 id="code-example-running-a-browser-task-with-the-skyvern-python-sdk">Code Example: Running a Browser Task with the Skyvern Python SDK</h2>



Most browser automations eventually hit a wall with login-protected pages, dynamic content, or multi-step flows that a selector-based tools cannot handle reliably. Here is what triggering a production browser workflow through Skyvern looks like using the Python SDK:



<pre><code class="language-python">pip install skyvern
</code></pre>





<pre><code class="language-python">from skyvern import Skyvern
import asyncio

# Initialize the client with your Skyvern Cloud API key
skyvern = Skyvern(api_key="YOUR_SKYVERN_API_KEY")

async def run_invoice_download():
    task = await skyvern.run_task(
        # Starting URL for the workflow
        url="https://portal.example.com/invoices",
        # Natural language goal — no XPaths or selectors needed
        prompt="Log in, go to the invoices page, and download the most recent invoice. "
               "COMPLETE when the file has been downloaded successfully.",
        # Define the output schema so the response is structured JSON
        data_extraction_schema={
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "The invoice number from the downloaded file"
                },
                "amount_due": {
                    "type": "number",
                    "description": "Total amount due on the invoice"
                },
                "due_date": {
                    "type": "string",
                    "description": "Payment due date in YYYY-MM-DD format"
                }
            }
        },
        # Block until the task finishes before reading output
        wait_for_completion=True,
        # Pass a totp_identifier if the portal requires 2FA
        totp_identifier="invoices@yourcompany.com",
        # Receive a webhook notification when the run completes
        webhook_url="https://your-server.com/webhooks/skyvern",
    )
    # Structured output matching the schema above
    print(task.output)

asyncio.run(run_invoice_download())
</code></pre>



No element IDs, no XPaths, no CSS selectors in sight. The agent reads the live page state, moves through the login flow, handles 2FA through the `totp_identifier`, and extracts structured data, all without a script that breaks the moment the portal updates its layout. Swap the `url` and `prompt` to point at a different site and the same code runs unchanged.



<h2 id="final-thoughts-on-browser-automation-through-mcp">Final Thoughts on Browser Automation Through MCP</h2>



<a href="https://www.skyvern.com/" rel="dofollow">Browser MCP</a> solves the problem of brittle automation scripts by letting AI agents read and interact with web pages the way a person would, instead of relying on selectors that break every time a site updates. Your choice of server depends on what you're building, whether you need simple development feedback or production workflows that handle authentication and dynamic content across multiple sites. Start with a basic setup to see how browser MCP fits your workflows, then move to something more capable when you need reliability at scale. <a href="https://meetings.hubspot.com/skyvern/demo?uuid=7c83865f-1a92-4c44-9e52-1ba0dbc04f7a" rel="dofollow">Book time with us</a> if you want to talk through what production-grade browser automation looks like for your specific use case.



<h2 id="faq">FAQ</h2>





<h3 id="browser-mcp-vs-playwright-which-should-you-use">Browser MCP vs Playwright: which should you use?</h3>



Playwright is faster for repetitive tasks where the page structure stays the same, but browser MCP handles dynamic content and UI changes without breaking. If your workflows involve sites that update frequently or require judgment calls, browser MCP adapts where Playwright scripts fail.



<h3 id="can-browser-mcp-handle-2fa-and-captcha-automatically">Can browser MCP handle 2FA and CAPTCHA automatically?</h3>



Most browser MCP servers expose the browser state but leave authentication handling to your AI agent, which often struggles with CAPTCHA and multi-factor flows. Skyvern's MCP server includes built-in authentication handling that covers OAuth, MFA prompts, and CAPTCHA solving without requiring custom workarounds.



<h3 id="how-do-you-install-browser-mcp-in-cursor">How do you install browser MCP in Cursor?</h3>



Edit `~/.cursor/mcp.json` and add your browser MCP server under the `mcpServers` key with the command path and arguments. Save the file, restart Cursor, and verify the connection by asking Cursor's AI to take a screenshot or open a URL.



<h3 id="whats-the-best-browser-mcp-server-for-production-workflows">What's the best browser MCP server for production workflows?</h3>



For development and debugging, agentdeskai/browser-tools-mcp works well with Chrome. For production workflows that need to handle authentication complexity, dynamic sites, and ongoing maintenance, Skyvern's MCP server uses computer vision instead of DOM selectors, so automations keep working when websites change their layouts.
