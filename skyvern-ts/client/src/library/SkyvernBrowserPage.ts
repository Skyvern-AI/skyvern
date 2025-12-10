import type { Page } from "playwright";
import type { SkyvernBrowser } from "./SkyvernBrowser.js";
import { SkyvernBrowserPageAgent } from "./SkyvernBrowserPageAgent.js";
import { SkyvernBrowserPageAi } from "./SkyvernBrowserPageAi.js";

export class SkyvernBrowserPageCore {
    private readonly _browser: SkyvernBrowser;
    private readonly _page: Page;
    private readonly _ai: SkyvernBrowserPageAi;
    public readonly agent: SkyvernBrowserPageAgent;
    private readonly _proxy: SkyvernBrowserPage;

    private constructor(browser: SkyvernBrowser, page: Page) {
        this._browser = browser;
        this._page = page;
        this._ai = new SkyvernBrowserPageAi(browser, page);
        this.agent = new SkyvernBrowserPageAgent(browser, page);

        this._proxy = new Proxy(this, {
            get(target, prop, receiver) {
                if (prop in target) {
                    return Reflect.get(target, prop, receiver);
                }

                const value = Reflect.get(target._page, prop, target._page);
                if (typeof value === "function") {
                    return value.bind(target._page);
                }
                return value;
            },
        }) as unknown as SkyvernBrowserPage;
    }

    static create(browser: SkyvernBrowser, page: Page): SkyvernBrowserPage {
        const instance = new SkyvernBrowserPageCore(browser, page);
        return instance._proxy;
    }

    get page(): Page {
        return this._page;
    }

    get browser(): SkyvernBrowser {
        return this._browser;
    }

    async click(selector: string, options?: Parameters<Page["click"]>[1]): Promise<void>;
    async click(options: { prompt: string } & Partial<Parameters<Page["click"]>[1]>): Promise<void>;
    async click(
        selectorOrOptions: string | ({ prompt: string } & Partial<Parameters<Page["click"]>[1]>),
        options?: Parameters<Page["click"]>[1],
    ): Promise<void> {
        if (typeof selectorOrOptions === "string") {
            return this._page.click(selectorOrOptions, options);
        } else {
            const { prompt, timeout, ...data } = selectorOrOptions;
            await this._ai.aiClick({
                intention: prompt,
                timeout,
                data: Object.keys(data).length > 0 ? data : undefined,
            });
        }
    }

    async fill(selector: string, value: string, options?: Parameters<Page["fill"]>[2]): Promise<void>;
    async fill(options: { prompt: string; value?: string } & Partial<Parameters<Page["fill"]>[2]>): Promise<void>;
    async fill(
        selectorOrOptions: string | ({ prompt: string; value?: string } & Partial<Parameters<Page["fill"]>[2]>),
        value?: string,
        options?: Parameters<Page["fill"]>[2],
    ): Promise<void> {
        if (typeof selectorOrOptions === "string") {
            if (value === undefined) {
                throw new Error("value is required when selector is provided");
            }
            return this._page.fill(selectorOrOptions, value, options);
        } else {
            const { prompt, value: fillValue, timeout, ...data } = selectorOrOptions;
            await this._ai.aiInputText({
                value: fillValue,
                intention: prompt,
                timeout,
                data: Object.keys(data).length > 0 ? data : undefined,
            });
        }
    }

    async selectOption(
        selector: string,
        values: string | string[],
        options?: Parameters<Page["selectOption"]>[2],
    ): Promise<string[]>;
    async selectOption(
        options: { prompt: string; value?: string } & Partial<Parameters<Page["selectOption"]>[2]>,
    ): Promise<string[]>;
    async selectOption(
        selectorOrOptions: string | ({ prompt: string; value?: string } & Partial<Parameters<Page["selectOption"]>[2]>),
        values?: string | string[],
        options?: Parameters<Page["selectOption"]>[2],
    ): Promise<string[]> {
        if (typeof selectorOrOptions === "string") {
            if (values === undefined) {
                throw new Error("value is required when selector is provided");
            }
            return this._page.selectOption(selectorOrOptions, values, options);
        } else {
            const { prompt, value, timeout, ...data } = selectorOrOptions;
            await this._ai.aiSelectOption({
                value,
                intention: prompt,
                timeout,
                data: Object.keys(data).length > 0 ? data : undefined,
            });
            return value ? [value] : [];
        }
    }

    async act(prompt: string): Promise<void> {
        return this._ai.aiAct(prompt);
    }

    async extract(options: {
        prompt: string;
        schema?: Record<string, unknown> | unknown[] | string;
        errorCodeMapping?: Record<string, string>;
        intention?: string;
        data?: string | Record<string, unknown>;
    }): Promise<Record<string, unknown> | unknown[] | string | null> {
        return this._ai.aiExtract(options);
    }

    async validate(prompt: string, model?: Record<string, unknown> | string): Promise<boolean> {
        const normalizedModel: Record<string, unknown> | undefined =
            typeof model === "string" ? { modelName: model } : model;
        return this._ai.aiValidate({ prompt, model: normalizedModel });
    }

    async prompt(
        prompt: string,
        schema?: Record<string, unknown>,
        model?: Record<string, unknown> | string,
    ): Promise<Record<string, unknown> | unknown[] | string | null> {
        const normalizedModel: Record<string, unknown> | undefined =
            typeof model === "string" ? { modelName: model } : model;
        return this._ai.aiPrompt({ prompt, schema, model: normalizedModel });
    }
}

export type SkyvernBrowserPage = SkyvernBrowserPageCore & Page;
