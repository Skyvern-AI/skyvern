import type { Browser, BrowserContext, Page } from "playwright";
import type { Skyvern } from "./Skyvern.js";
import { SkyvernBrowserPageCore, type SkyvernBrowserPage } from "./SkyvernBrowserPage.js";

/**
 * A browser context wrapper that creates Skyvern-enabled pages.
 *
 * This class wraps a Playwright BrowserContext and provides methods to create
 * SkyvernBrowserPage instances that combine traditional browser automation with
 * AI-powered task execution capabilities. It manages browser session state and
 * enables persistent browser sessions across multiple pages.
 *
 * @example
 * ```typescript
 * const skyvern = Skyvern.local();
 * const browser = await skyvern.launchCloudBrowser();
 *
 * // Get or create the working page
 * const page = await browser.getWorkingPage();
 *
 * // Create a new page
 * const newPage = await browser.newPage();
 * ```
 */
export class SkyvernBrowser {
    private readonly _skyvern: Skyvern;
    private readonly _browserContext: BrowserContext;
    private readonly _browser?: Browser;
    private readonly _browserSessionId?: string;
    private readonly _browserAddress?: string;

    public workflowRunId?: string;

    constructor(
        skyvern: Skyvern,
        browserContext: BrowserContext,
        options?: {
            browser?: Browser;
            browserSessionId?: string;
            browserAddress?: string;
        },
    ) {
        this._skyvern = skyvern;
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

    get skyvern(): Skyvern {
        return this._skyvern;
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
     * @returns SkyvernBrowserPage: The most recent page wrapped with Skyvern capabilities.
     */
    async getWorkingPage(): Promise<SkyvernBrowserPage> {
        const pages = this._browserContext.pages();
        const page = pages.length > 0 ? pages[pages.length - 1] : await this._browserContext.newPage();
        return this._createSkyvernPage(page);
    }

    /**
     * Create a new page (tab) in the browser context.
     *
     * This method always creates a new page, similar to opening a new tab in a browser.
     * The new page will have both Playwright's standard API and Skyvern's AI capabilities.
     *
     * @returns SkyvernBrowserPage: A new page wrapped with Skyvern capabilities.
     */
    async newPage(): Promise<SkyvernBrowserPage> {
        const page = await this._browserContext.newPage();
        return this._createSkyvernPage(page);
    }

    pages(): SkyvernBrowserPage[] {
        return this._browserContext.pages().map((page) => SkyvernBrowserPageCore.create(this, page));
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
     * const browser = await skyvern.launchCloudBrowser();
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
            await this._skyvern.closeBrowserSession(this._browserSessionId);
        }

        this._skyvern._untrackBrowser(this);
    }

    private _createSkyvernPage(page: Page): SkyvernBrowserPage {
        return SkyvernBrowserPageCore.create(this, page);
    }
}
