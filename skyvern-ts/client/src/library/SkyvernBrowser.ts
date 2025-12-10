import type { Browser, BrowserContext, Page } from "playwright";
import type { Skyvern } from "./Skyvern.js";
import { SkyvernBrowserPageCore, type SkyvernBrowserPage } from "./SkyvernBrowserPage.js";

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

    async getWorkingPage(): Promise<SkyvernBrowserPage> {
        const pages = this._browserContext.pages();
        const page = pages.length > 0 ? pages[pages.length - 1] : await this._browserContext.newPage();
        return this._createSkyvernPage(page);
    }

    async newPage(): Promise<SkyvernBrowserPage> {
        const page = await this._browserContext.newPage();
        return this._createSkyvernPage(page);
    }

    pages(): SkyvernBrowserPage[] {
        return this._browserContext.pages().map((page) => SkyvernBrowserPageCore.create(this, page));
    }

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
