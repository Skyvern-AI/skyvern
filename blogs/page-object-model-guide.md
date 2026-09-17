---
title: "Mastering Page Object Model (POM) in Test Automation (Updated July 2026)"
description: "Master Page Object Model implementation in Selenium, Playwright, and Cypress. Learn POM vs Page Factory, best practices, and modern AI alternatives. July 2026."
excerpt: "A developer renames a button ID in a morning deploy. By afternoon, your test suite has 47 failures. Every one traces to the same cause: a locator string that no longer matches anything on the page. You update the page object, find the four test files that imported it, update each one, and run the suite again. This is the maintenance loop that Page Object Model teams live in. Not because POM is poorly designed, but because it encodes assumptions about the UI that developers keep invalidating. Mos"
slug: "page-object-model-guide"
publicationState: "published"
publishedAt: "2025-10-31T04:35:00.000Z"
updatedAt: "2026-08-01T00:15:02.000Z"
author: "suchintan-2"
tags: []
featureImage: "https://dcbllm8dvghjo.cloudfront.net/media/blog/d37af9b3a7bd31d9263cbf46be65a97c0aa1874eec074474ca3ae322c7bdce92-c1m3puyone3dltsiwn2jz.png"
featureImageAlt: null
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: true
seoTitle: "Page Object Model Guide: Best Practices Updated July 2026"
ogTitle: "Page Object Model Guide: Best Practices Updated July 2026"
---
A developer renames a button ID in a morning deploy. By afternoon, your test suite has 47 failures. Every one traces to the same cause: a locator string that no longer matches anything on the page. You update the page object, find the four test files that imported it, update each one, and run the suite again. This is the maintenance loop that Page Object Model teams live in. Not because POM is poorly designed, but because it encodes assumptions about the UI that developers keep invalidating. Most teams accept it as part of the process. The Page Object Model has been the go-to pattern for test automation, and those brittle locators and maintenance cycles are a real drain on team productivity. <a href="https://www.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern's MCP server</a> is available to all users by default regardless of tier, and about 90% of Skyvern customers now create and run automations through MCP or API instead of the visual builder. That shift points to a different way of thinking about automation. Let's look at how to master POM and understand when it makes sense to move past it.

**TLDR:**

-   Page Object Model creates maintainable test code by treating each web page as a separate class with encapsulated elements and interactions
-   POM reduces maintenance overhead since UI changes only require updates in page classes, not every test script
-   Page Factory enhances traditional POM through @FindBy annotations and lazy loading for better performance in Selenium
-   Modern frameworks like Playwright and Cypress offer improved POM implementation with auto-waiting and resilient locators
-   Skyvern eliminates POM's brittle locator problems entirely using AI and computer vision to understand web pages contextually



<h2 id="page-object-model-fundamentals">Page Object Model Fundamentals</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/8850100d044e49b63340510b0c649ef755ea17bae4a129ad4e0ba19f0960f56b-wrblnsyx5rwxoddxjbdfc.webp" class="kg-image" alt="1_Uz0xBEbnd7IhEubY392Cow.png" loading="lazy"></figure>



The <a href="https://medium.com/@ahmetkemalyetkin/the-page-object-model-advantages-and-disadvantages-aa03a484349c?ref=skyvern.com" rel="dofollow">Page Object Model is a design pattern</a> that changes how we approach test automation by creating structured, maintainable code. At its core, POM treats each web page as a separate class, encapsulating all elements and interactions within that page into a single, reusable object.

Think of POM as creating a blueprint for every page in your application. Each page class contains the web elements (buttons, forms, links) and the methods that interact with those elements. This creates an object repository where testers can easily locate and manipulate page components without digging into complex HTML structures. POM separates test logic from page structure, making your automation framework more resilient to UI changes and easier to maintain over time.

The pattern works by defining three key components: pages, elements, and actions. Pages represent an entire DOM or major sections, elements are the individual components like input fields or buttons, and actions are the methods that perform operations on those elements. This approach has become the gold standard across automation frameworks like Selenium, Playwright, and Cypress. Teams adopt POM because it promotes code reusability, reduces duplication, and creates a clear separation between test scripts and page-specific code.

Traditional POM, though, faces challenges with brittle locators and maintenance overhead as web applications become more complex and interactive. Modern AI browser automation tools are coming up to solve these limitations through computer vision and intelligent element detection.



<h2 id="advantages-and-disadvantages-of-page-object-model">Advantages and Disadvantages of Page Object Model</h2>



POM provides a number of advantages for test automation teams:

-   <a href="https://medium.com/@ahmetkemalyetkin/the-page-object-model-advantages-and-disadvantages-aa03a484349c?ref=skyvern.com" rel="dofollow"><strong>Easier test maintenance</strong></a>. When UI elements change, you only update the page class instead of every test that uses those elements.
-   <strong>Code reusability</strong>. Once you create a page object, multiple test cases can use the same methods and elements. This eliminates duplicate code and creates a single source of truth for page interactions.
-   <strong>Test readability</strong>. This improves dramatically with POM. Your test scripts become more intuitive, reading like business workflows instead of technical implementations. New team members can understand test logic without deciphering complex locator strategies. POM creates a clear separation between test logic and page structure, making your automation framework more professional and scalable.

POM, though, introduces notable challenges:

-   <a href="https://medium.com/@ahmetkemalyetkin/the-page-object-model-advantages-and-disadvantages-aa03a484349c?ref=skyvern.com" rel="dofollow"><strong>Additional complexity</strong>.</a> This shows up during initial setup, requiring more time and effort from your team. The learning curve can be steep for beginners who must understand both the pattern and the underlying automation framework.
-   <strong>Over-engineering</strong>. This challenge becomes a risk with simple applications. Small projects might not warrant the overhead of creating extensive page object hierarchies. The pattern works best for medium to large applications with multiple pages and complex user interactions.
-   <strong>Maintenance overhead</strong>. This still exists despite the benefits. Brittle locators can break across multiple page objects, and common automation mistakes can compound when replicated throughout your page object structure.



<h2 id="page-object-model-vs-page-factory">Page Object Model vs Page Factory</h2>



Page Factory is an enhanced implementation of the traditional Page Object Model. While POM provides the conceptual framework, Page Factory is an extension of <a href="https://www.browserstack.com/guide/page-object-model-in-selenium?ref=skyvern.com" rel="dofollow">POM in Selenium</a> that uses annotations like @FindBy to initialize web elements at runtime, simplifying object creation and improving test readability.

The core difference is that traditional POM requires manual element location using driver.findElement() calls throughout your code. Page Factory automates this process through annotations, reducing boilerplate code by a lot.

Performance distinguishes these approaches in one specific way. Page Factory defers element lookup until first access (lazy loading), which can reduce unnecessary WebDriver calls at instantiation time. Elements are only located when actually needed, instead of during page object instantiation. The table below provides an overview of the features and the differences between POM and Page Factory.



<!--kg-card-begin: html-->
<table class="border-collapse table-fixed w-full max-w-full" style="border-collapse: collapse; width: 100%; min-width: 150px"><tbody><tr class=""><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Feature</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Page Object Model</p></th><th colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #f9fafb; color: #000000; padding: 12px; text-align: left; font-size: 14px"><p>Page Factory</p></th></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Element Initialization</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Manual using driver.findElement()</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Automatic using @FindBy annotation</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Performance</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Standard element lookup</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Lazy loading with better performance</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Code Complexity</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>More boilerplate code</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Cleaner, annotation-based code</p></td></tr><tr class=""><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Maintenance</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Higher maintenance overhead</p></td><td colspan="1" rowspan="1" style="border: 1px solid #d1d5db; background-color: #ffffff; color: #000000; padding: 12px; font-size: 14px"><p>Lower maintenance with annotations</p></td></tr></tbody></table>
<!--kg-card-end: html-->



> Page Factory's lazy loading approach means elements are located only when accessed, improving performance and reducing unnecessary web driver calls.

When should you choose between the two? Choose traditional POM when working with non-Selenium frameworks or when you need complete control over element initialization timing. Page Factory works best for Selenium-based projects where you want cleaner code and better performance. Both approaches, though, still face challenges with brittle locators and maintenance overhead. <a href="https://www.skyvern.com/blog/selenium-reviews-and-alternatives-2025" rel="dofollow">Selenium alternatives</a> tackle these limitations through AI-powered element detection and adaptive locator strategies.



<h2 id="implementing-pom-in-selenium">Implementing POM in Selenium</h2>



Setting up POM in Selenium requires a structured approach that separates page logic from test implementation.

The foundation begins with a base page class that contains common functionality shared across all pages. This base class typically includes WebDriver initialization, common wait methods, and utility functions for element interactions. Every specific page class extends this base class, inheriting shared behaviors while implementing page-specific elements and actions.

Page Factory offers a better approach by using `@FindBy` annotations that automatically initialize elements when the page object is created.

To start, create a project hierarchy with dedicated folders for pages, tests, and utilities. This organization keeps your automation framework scalable as your application grows. Implementing proper wait strategies within your page objects prevents flaky tests and handles changing content effectively.

For Java implementations, create separate packages for pages and tests. Python projects benefit from modules that group related page objects together. Both approaches should include configuration files for browser settings and test data management. Changing elements, though, require special handling within POM structures. To do this, use explicit waits in your page methods instead of hard-coded delays and implement retry mechanisms for elements that appear conditionally based on user actions or server responses.

Traditional POM implementation, though, still faces challenges with brittle locators and maintenance overhead. Modern approaches like Skyvern eliminate manual page object creation by using computer vision to understand web pages contextually, removing the dependency on fixed element locators entirely.



<h2 id="pom-with-playwright-implementation">POM with Playwright Implementation</h2>



Playwright brings major improvements to traditional POM implementation through its built-in auto-waiting features and modern locator strategies. Unlike Selenium's explicit wait requirements, Playwright page objects simplify authoring by creating higher-level APIs that naturally handle asynchronous operations without complex wait logic.

The foundation starts with creating page classes that accept a Playwright Page object in their constructor. This approach uses Playwright's page fixtures, making your page objects more testable and easier to manage across different browser contexts.

Playwright's locator strategy improves POM by providing resilient element selection. Use data-testid attributes, text content, or role-based selectors instead of fragile CSS selectors. This creates more stable page objects that resist UI changes. And, <a href="https://medium.com/@lucgagan/mastering-playwright-best-practices-for-web-automation-with-the-page-object-model-3541412b03d1?ref=skyvern.com" rel="dofollow">separating actions and assertions in page objects</a> makes tests more readable and guarantees reusability across different test scenarios.

How should you use Playwright?

-   First, organize your Playwright page objects using TypeScript modules for better type safety and IntelliSense support.
-   Second, create base page classes that handle common functionality like navigation and error handling, then extend them for specific page implementations.

Despite these improvements, traditional POM still requires manual maintenance of locators and page structures. Modern Playwright alternatives like Skyvern eliminate this overhead entirely through AI-powered web understanding.



<h2 id="pom-with-cypress-best-practices">POM with Cypress Best Practices</h2>



Cypress presents a unique challenge for POM implementation due to its architecture and built-in features. While POM is the most commonly used test automation method, Cypress advocates for App Actions as an alternative approach that directly calls application methods instead of interacting through the UI.

The debate focuses on abstraction levels. POM provides a high level of abstraction where tests can be written without low-level implementation details, making them easier to maintain and consistently reliable. App Actions offer faster execution by bypassing UI interactions entirely. So when should you choose POM over App Actions? In short, choose POM when your team needs consistent patterns across multiple testing frameworks or when testing complex user workflows that require UI validation. App Actions, though, work better for setup operations like user authentication or data preparation where UI interaction adds unnecessary overhead.

> Cypress's automatic waiting behavior eliminates the need for explicit wait methods in your page objects, simplifying implementation compared to other frameworks.

If you feel that Cypress is the tool for you, follow these recommendations:

-   Structure your Cypress page objects using ES6 classes with methods that return chainable Cypress commands.
-   Integrate custom commands within page objects to extend Cypress functionality while maintaining the POM pattern.
-   Store selectors as class properties to centralize element management.
-   Create a pages folder within your cypress/support directory.
-   Export page classes as modules and import them into your test files. This organization gives you maintainable test cases where selector changes only require updates in the page object file.

Both POM and App Actions, though, still require manual maintenance and brittle selector management. Modern browser automation tools eliminate these challenges through AI-powered element detection that adapts to UI changes automatically.



<h2 id="when-to-move-beyond-page-object-model">When to Move Beyond Page Object Model</h2>



POM reaches its breaking point when applications become highly changeable or when maintenance costs exceed testing benefits. The most obvious time to move beyond POM is when you are spending more time updating page objects than writing actual tests. This happens frequently with single-page applications where elements change based on user state or server responses.

Modern alternatives like the Facade Design Pattern focus on creating full facades at the test level, providing objects with all necessary inputs without maintaining individual page structures.

The screenplay pattern offers another evolution beyond POM by modeling tests as actors performing tasks instead of pages containing elements. This approach better represents user behavior and reduces coupling between tests and UI implementation details.

Component-based testing works well for applications built with modern frameworks like React or Vue. Instead of modeling entire pages, you test individual components in isolation, reducing complexity and improving test reliability.

Visual AI approaches, though, represent a structural category shift. There is a lot of architectural distance between selector-based POM and visual AI: one stores locator strings that developers keep invalidating, the other reads pages by appearance and context at runtime. These tools understand web pages contextually without relying on predetermined selectors. Skyvern's agent-based approach, for example, shows how AI can perceive and interact with web elements without manual page object creation, removing the brittleness that makes POM unsuitable for changing applications.



<h2 id="how-skyvern-changes-browser-automation-beyond-pom">How Skyvern Changes Browser Automation Beyond POM</h2>





<figure class="kg-card kg-image-card"><img src="https://dcbllm8dvghjo.cloudfront.net/media/blog/22a8b7ef1743cb3885dde04b1da3bea75b6427fdb067f16b1c1761ec757be2df-05ewydtosstiaooxslkog.png" class="kg-image" alt="skyvern.png" loading="lazy"></figure>



Traditional POM approaches crumble when faced with changing web applications and constant UI changes. <a href="https://www.skyvern.com/?ref=skyvern.com" rel="dofollow">Skyvern eliminates these limitations entirely</a> by using LLMs and computer vision to understand web pages contextually, removing the dependency on brittle locators that plague conventional automation frameworks.

Unlike POM's rigid page object structures, Skyvern operates on websites it has never seen before without requiring customized code. The system perceives web elements through visual understanding instead of predetermined XPaths or CSS selectors, making it naturally resistant to website layout changes. This approach solves POM's core challenges immediately. No more maintaining extensive page object hierarchies. No more updating locators when developers change element attributes. No more brittle automation scripts that break with every UI update.

Skyvern has also moved to a code-first workflow architecture where automations are built as executable Python code blocks instead of prompt-based task chains. The first time an agent runs a workflow, it records a full action path and compiles it into deterministic Playwright code. Every subsequent run executes that compiled code without invoking the LLM, delivering faster execution and lower token costs. Non-technical users see screenshots and timeline steps in the interface, not raw Python. When a portal changes and the compiled code can no longer complete the task, the system falls back to the agent, re-learns the updated path, and regenerates the code automatically. This eliminates the brittleness of traditional POM without requiring anyone to write or maintain selectors.

Consider a procurement workflow that spans multiple vendor websites. Traditional POM requires creating separate page objects for each vendor's unique interface, then maintaining those objects as sites evolve. Skyvern automates these workflows using the same logic across different websites, adapting to each interface automatically. The proxy infrastructure includes both residential and ISP proxies with country, state, and city-level targeting, all integrated as one system with the automation logic instead of requiring separate proxy management. The multi-agent system reasons through complex interactions like form filling, authentication, and data extraction without predefined element mappings. For credential management within these workflows, Skyvern supports native Bitwarden integration (available on the enterprise plan), allowing users to reference Bitwarden-stored credentials directly without manual entry. This contextual understanding changes browser automation from a maintenance-heavy coding exercise into a simple workflow definition process.



<h3 id="skyvern-in-practice-what-replacing-a-page-object-looks-like">Skyvern in Practice: What Replacing a Page Object Looks Like</h3>



Here is what automating a vendor portal workflow looks like with the Skyvern Python SDK. The task below logs in, works through to the invoices page, and returns structured data, with no element IDs, no CSS selectors, and no XPaths anywhere in the code:



<pre><code class="language-python">from skyvern import Skyvern
import asyncio

# Initialize the client with your API key
skyvern = Skyvern(api_key="YOUR_API_KEY")

async def check_invoices():
    # Describe the goal in plain language — no locators required
    result = await skyvern.run_task(
        url="https://your-vendor-portal.com",
        prompt=(
            "Log in with the provided credentials, go to the invoices page, "
            "and extract the three most recent invoice numbers and their amounts. "
            "COMPLETE when the invoice data has been extracted."
        ),
        # Define the output shape you want back
        data_extraction_schema={
            "type": "object",
            "properties": {
                "invoices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "invoice_number": {"type": "string"},
                            "amount": {"type": "string"}
                        }
                    }
                }
            }
        },
        wait_for_completion=True,  # Block until the task finishes
    )
    return result.output

asyncio.run(check_invoices())
</code></pre>



No page object. No locator strings. No driver.findElement() calls. The same code works across different vendor portals without any per-site modifications, and if a portal redesigns its invoice page tomorrow, nothing in this code breaks because there is nothing hardcoded to break.

Skyvern also supports role-based access control and approval gates at the workflow level. Organizations can restrict which users have permission to run specific workflows and require explicit approval before executing sensitive automations like financial transactions or government filings.

For teams struggling with POM's brittleness and overhead, Skyvern represents the next evolution in browser automation technology.



<h2 id="final-thoughts-on-page-object-model-implementation-and-alternatives">Final thoughts on Page Object Model implementation and alternatives</h2>



The Page Object Model has served automation teams well, but its brittleness with changing web applications creates real challenges that slow down development. POM still works well in specific contexts: small projects with stable interfaces, applications that change infrequently, and teams with large existing POM investments where migration costs outweigh the ongoing maintenance burden. For those teams, POM remains a solid choice. Modern websites, though, change too frequently for manual locator maintenance to remain practical at scale. Skyvern eliminates these limitations entirely by <a href="https://www.skyvern.com/?ref=skyvern.com" rel="dofollow">understanding web pages through AI</a> instead of predetermined selectors, letting you focus on building workflows instead of fixing broken tests. Your automation strategy should evolve with the technology available to make your team more productive.



<h2 id="faq">FAQ</h2>





<h3 id="how-do-i-decide-between-traditional-pom-and-page-factory-for-my-selenium-project">How do I decide between traditional POM and Page Factory for my Selenium project?</h3>



Choose Page Factory for most Selenium projects as it offers better performance through lazy loading and cleaner code with @FindBy annotations. Use traditional POM only when you need complete control over element initialization timing or when working with non-Selenium frameworks.



<h3 id="whats-the-main-difference-between-pom-implementation-in-playwright-versus-selenium">What's the main difference between POM implementation in Playwright versus Selenium?</h3>



Playwright simplifies POM implementation by providing built-in auto-waiting features and resilient locators, eliminating the need for explicit wait methods. Selenium requires manual wait strategies and is more prone to brittle XPath-based interactions that break with website changes.



<h3 id="when-should-i-consider-moving-beyond-page-object-model-entirely">When should I consider moving beyond Page Object Model entirely?</h3>



Consider alternatives when you spend more time maintaining page objects than writing tests, typically with highly changing single-page applications. If UI changes consistently break multiple page objects simultaneously, modern AI-powered tools like Skyvern can remove locator brittleness entirely.



<h3 id="can-i-use-page-object-model-effectively-with-cypress-or-should-i-use-app-actions-instead">Can I use Page Object Model effectively with Cypress, or should I use App Actions instead?</h3>



Use POM for complex user workflows requiring UI validation and when maintaining consistency across multiple testing frameworks. Choose App Actions for setup operations like authentication where UI interaction adds unnecessary overhead and faster execution is preferred.



<h3 id="how-does-ai-browser-automation-eliminate-the-problems-i-face-with-traditional-pom-maintenance">How does AI browser automation eliminate the problems I face with traditional POM maintenance?</h3>



AI-powered tools understand web pages contextually through computer vision instead of predetermined selectors, automatically adapting to UI changes without manual page object updates. This eliminates the brittle locator management that makes traditional POM maintenance-heavy for changing applications.
