import type { Browser, BrowserContext, Page } from "playwright";
import type { Testcharmvision } from "./Testcharmvision.js";
import { TestcharmvisionBrowserPageCore, type TestcharmvisionBrowserPage } from "./TestcharmvisionBrowserPage.js";

/**
 * A browser context wrapper that creates Testcharmvision-enabled pages.
 *
 * This class wraps a Playwright BrowserContext and provides methods to create
 * TestcharmvisionBrowserPage instances that combine traditional browser automation with
 * AI-powered task execution capabilities. It manages browser session state and
 * enables persistent browser sessions across multiple pages.
 *
 * @example
 * ```typescript
 * const testcharmvision = Testcharmvision.local();
 * const browser = await testcharmvision.launchCloudBrowser();
 *
 * // Get or create the working page
 * const page = await browser.getWorkingPage();
 *
 * // Create a new page
 * const newPage = await browser.newPage();
 * ```
 */
export class TestcharmvisionBrowser {
    private readonly _testcharmvision: Testcharmvision;
    private readonly _browserContext: BrowserContext;
    private readonly _browser?: Browser;
    private readonly _browserSessionId?: string;
    private readonly _browserAddress?: string;

    public workflowRunId?: string;

    constructor(
        testcharmvision: Testcharmvision,
        browserContext: BrowserContext,
        options?: {
            browser?: Browser;
            browserSessionId?: string;
            browserAddress?: string;
        },
    ) {
        this._testcharmvision = testcharmvision;
        this._browserContext = browserContext;
        this._browser = options?.browser;
        this._browserSessionId = options?.browserSessionId;
        this._browserAddress = options?.browserAddress;
    }

    get browserSessionId(): string | undefined {
        return this._browserSessionId;
    }

    get browserAddress(): string | undefined {
        return this._browserAddress;
    }

    get testcharmvision(): Testcharmvision {
        return this._testcharmvision;
    }

    get context(): BrowserContext {
        return this._browserContext;
    }

    /**
     * Get the most recent page or create a new one if none exists.
     *
     * This method returns the last page in the browser context, or creates a new page
     * if the context has no pages. This is useful for continuing work on an existing
     * page without creating unnecessary new tabs.
     *
     * @returns TestcharmvisionBrowserPage: The most recent page wrapped with Testcharmvision capabilities.
     */
    async getWorkingPage(): Promise<TestcharmvisionBrowserPage> {
        const pages = this._browserContext.pages();
        const page = pages.length > 0 ? pages[pages.length - 1] : await this._browserContext.newPage();
        return this._createTestcharmvisionPage(page);
    }

    /**
     * Create a new page (tab) in the browser context.
     *
     * This method always creates a new page, similar to opening a new tab in a browser.
     * The new page will have both Playwright's standard API and Testcharmvision's AI capabilities.
     *
     * @returns TestcharmvisionBrowserPage: A new page wrapped with Testcharmvision capabilities.
     */
    async newPage(): Promise<TestcharmvisionBrowserPage> {
        const page = await this._browserContext.newPage();
        return this._createTestcharmvisionPage(page);
    }

    pages(): TestcharmvisionBrowserPage[] {
        return this._browserContext.pages().map((page) => TestcharmvisionBrowserPageCore.create(this, page));
    }

    /**
     * Close the browser and optionally close the browser session.
     *
     * This method closes the browser context. If the browser is associated with a
     * cloud browser session (has a browserSessionId), it will also close the
     * browser session via the API, marking it as completed.
     *
     * @example
     * ```typescript
     * const browser = await testcharmvision.launchCloudBrowser();
     * // ... use the browser ...
     * await browser.close();  // Closes both browser and cloud session
     * ```
     */
    async close(): Promise<void> {
        if (this._browser) {
            await this._browser.close();
        } else {
            await this._browserContext.close();
        }

        if (this._browserSessionId) {
            await this._testcharmvision.closeBrowserSession(this._browserSessionId);
        }

        this._testcharmvision._untrackBrowser(this);
    }

    private _createTestcharmvisionPage(page: Page): TestcharmvisionBrowserPage {
        return TestcharmvisionBrowserPageCore.create(this, page);
    }
}
