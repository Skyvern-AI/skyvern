import type { Page } from "playwright";
import type { SkyvernBrowser } from "./SkyvernBrowser.js";
import { SkyvernBrowserPageAgent } from "./SkyvernBrowserPageAgent.js";
import { SkyvernBrowserPageAi } from "./SkyvernBrowserPageAi.js";

/**
 * A browser page wrapper that combines Playwright's page API with Skyvern's AI capabilities.
 *
 * This class provides a unified interface for both traditional browser automation (via Playwright)
 * and AI-powered task execution (via Skyvern). It exposes standard page methods like click, fill,
 * goto, etc., while also providing access to Skyvern's task and workflow execution through the
 * `agent` attribute.
 *
 * @example
 * ```typescript
 * // Use standard Playwright methods
 * await page.goto("https://example.com");
 * await page.fill("#username", "user@example.com");
 * await page.click("#login-button");
 *
 * // Or use Skyvern's AI capabilities
 * await page.agent.runTask("Fill out the contact form and submit it");
 * ```
 */
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

    /**
     * Click an element using a CSS selector, AI-powered prompt matching, or both.
     *
     * This method supports three modes:
     * - **Selector-based**: Click the element matching the CSS selector
     * - **AI-powered**: Use natural language to describe which element to click
     * - **Fallback mode**: Try the selector first, fall back to AI if it fails
     *
     * @param selector - CSS selector for the target element.
     * @param options - Click options including prompt.
     * @param options.prompt - Natural language description of which element to click.
     *
     * @example
     * ```typescript
     * // Click using a CSS selector
     * await page.click("#open-invoice-button");
     *
     * // Click using AI with natural language
     * await page.click({ prompt: "Click on the 'Open Invoice' button" });
     *
     * // Try selector first, fall back to AI if selector fails
     * await page.click("#open-invoice-button", { prompt: "Click on the 'Open Invoice' button" });
     * ```
     */
    async click(selector: string, options?: Parameters<Page["click"]>[1]): Promise<void>;
    async click(options: { prompt: string } & Partial<Parameters<Page["click"]>[1]>): Promise<void>;
    async click(
        selector: string,
        options: { prompt: string } & Partial<Parameters<Page["click"]>[1]>,
    ): Promise<void>;
    async click(
        selectorOrOptions?: string | ({ prompt: string } & Partial<Parameters<Page["click"]>[1]>),
        options?: Parameters<Page["click"]>[1] | ({ prompt?: string } & Partial<Parameters<Page["click"]>[1]>),
    ): Promise<void> {
        let selector: string | undefined;
        let prompt: string | undefined;
        let clickOptions: Partial<Parameters<Page["click"]>[1]> = {};
        let timeout: number | undefined;

        // Parse arguments
        if (typeof selectorOrOptions === "string") {
            selector = selectorOrOptions;
            if (options && typeof options === "object") {
                const { prompt: p, timeout: t, ...rest } = options as {
                    prompt?: string;
                    timeout?: number;
                } & Partial<Parameters<Page["click"]>[1]>;
                prompt = p;
                timeout = t;
                clickOptions = rest;
            } else if (options) {
                clickOptions = options;
            }
        } else if (selectorOrOptions && typeof selectorOrOptions === "object") {
            const { prompt: p, timeout: t, ...rest } = selectorOrOptions;
            prompt = p;
            timeout = t;
            clickOptions = rest;
        }

        if (!selector && !prompt) {
            throw new Error("Missing input: pass a selector and/or a prompt.");
        }

        // Try to click the element with the original selector first
        let errorToRaise: Error | undefined;
        if (selector) {
            try {
                await this._page.click(selector, { ...clickOptions, timeout });
                return;
            } catch (error) {
                errorToRaise = error as Error;
                selector = undefined;
            }
        }

        // If the original selector doesn't work, try to click the element with the AI generated selector
        if (prompt) {
            await this._ai.aiClick({
                intention: prompt,
                data: Object.keys(clickOptions).length > 0 ? clickOptions : undefined,
                timeout,
            });
            return;
        }

        if (errorToRaise) {
            throw errorToRaise;
        }
    }

    /**
     * Fill an input field using a CSS selector, AI-powered prompt matching, or both.
     *
     * This method supports three modes:
     * - **Selector-based**: Fill the input field with a value using CSS selector
     * - **AI-powered**: Use natural language prompt (AI extracts value from prompt or uses provided value)
     * - **Fallback mode**: Try the selector first, fall back to AI if it fails
     *
     * @param selector - CSS selector for the target input element.
     * @param value - The text value to input into the field.
     * @param options - Fill options including prompt.
     * @param options.prompt - Natural language description of which field to fill and what value.
     *
     * @example
     * ```typescript
     * // Fill using selector and value
     * await page.fill("#email-input", "user@example.com");
     *
     * // Fill using AI with natural language
     * await page.fill({ prompt: "Fill 'user@example.com' in the email address field" });
     *
     * // Try selector first, fall back to AI if selector fails
     * await page.fill("#email-input", "user@example.com", { prompt: "Fill the email address" });
     * ```
     */
    async fill(selector: string, value: string, options?: Parameters<Page["fill"]>[2]): Promise<void>;
    async fill(options: { prompt: string; value?: string } & Partial<Parameters<Page["fill"]>[2]>): Promise<void>;
    async fill(
        selector: string,
        value: string,
        options: { prompt: string } & Partial<Parameters<Page["fill"]>[2]>,
    ): Promise<void>;
    async fill(
        selectorOrOptions?: string | ({ prompt: string; value?: string } & Partial<Parameters<Page["fill"]>[2]>),
        value?: string,
        options?: Parameters<Page["fill"]>[2] | ({ prompt?: string } & Partial<Parameters<Page["fill"]>[2]>),
    ): Promise<void> {
        let selector: string | undefined;
        let fillValue: string | undefined;
        let prompt: string | undefined;
        let fillOptions: Partial<Parameters<Page["fill"]>[2]> = {};
        let timeout: number | undefined;

        if (typeof selectorOrOptions === "string") {
            selector = selectorOrOptions;
            fillValue = value;
            if (options && typeof options === "object") {
                const { prompt: p, timeout: t, ...rest } = options as {
                    prompt?: string;
                    timeout?: number;
                } & Partial<Parameters<Page["fill"]>[2]>;
                prompt = p;
                timeout = t;
                fillOptions = rest;
            } else if (options) {
                fillOptions = options;
            }
        } else if (selectorOrOptions && typeof selectorOrOptions === "object") {
            const { prompt: p, value: v, timeout: t, ...rest } = selectorOrOptions;
            prompt = p;
            fillValue = v;
            timeout = t;
            fillOptions = rest;
        }

        if (!selector && !prompt) {
            throw new Error("Missing input: pass a selector and/or a prompt.");
        }

        let errorToRaise: Error | undefined;
        if (selector && fillValue !== undefined) {
            try {
                await this._page.fill(selector, fillValue, { ...fillOptions, timeout });
                return;
            } catch (error) {
                errorToRaise = error as Error;
                selector = undefined;
            }
        }

        if (prompt) {
            await this._ai.aiInputText({
                value: fillValue,
                intention: prompt,
                data: Object.keys(fillOptions).length > 0 ? fillOptions : undefined,
                timeout,
            });
            return;
        }

        if (errorToRaise) {
            throw errorToRaise;
        }
    }

    /**
     * Select an option from a dropdown using a CSS selector, AI-powered prompt matching, or both.
     *
     * This method supports three modes:
     * - **Selector-based**: Select the option with a value using CSS selector
     * - **AI-powered**: Use natural language prompt (AI extracts value from prompt or uses provided value)
     * - **Fallback mode**: Try the selector first, fall back to AI if it fails
     *
     * @param selector - CSS selector for the target select/dropdown element.
     * @param value - The option value to select.
     * @param options - Select options including prompt.
     * @param options.prompt - Natural language description of which option to select.
     *
     * @example
     * ```typescript
     * // Select using selector and value
     * await page.selectOption("#country", "us");
     *
     * // Select using AI with natural language
     * await page.selectOption({ prompt: "Select 'United States' from the country dropdown" });
     *
     * // Try selector first, fall back to AI if selector fails
     * await page.selectOption("#country", "us", { prompt: "Select United States from country" });
     * ```
     */
    async selectOption(
        selector: string,
        values: string | string[],
        options?: Parameters<Page["selectOption"]>[2],
    ): Promise<void>;
    async selectOption(
        options: { prompt: string; value?: string } & Partial<Parameters<Page["selectOption"]>[2]>,
    ): Promise<void>;
    async selectOption(
        selector: string,
        values: string | string[],
        options: { prompt: string } & Partial<Parameters<Page["selectOption"]>[2]>,
    ): Promise<void>;
    async selectOption(
        selectorOrOptions?:
            | string
            | ({ prompt: string; value?: string } & Partial<Parameters<Page["selectOption"]>[2]>),
        values?: string | string[],
        options?: Parameters<Page["selectOption"]>[2] | ({ prompt?: string } & Partial<Parameters<Page["selectOption"]>[2]>),
    ): Promise<void> {
        let selector: string | undefined;
        let selectValue: string | string[] | undefined;
        let prompt: string | undefined;
        let selectOptions: Partial<Parameters<Page["selectOption"]>[2]> = {};
        let timeout: number | undefined;

        // Parse arguments
        if (typeof selectorOrOptions === "string") {
            selector = selectorOrOptions;
            selectValue = values;
            if (options && typeof options === "object") {
                const { prompt: p, timeout: t, ...rest } = options as {
                    prompt?: string;
                    timeout?: number;
                } & Partial<Parameters<Page["selectOption"]>[2]>;
                prompt = p;
                timeout = t;
                selectOptions = rest;
            } else if (options) {
                selectOptions = options;
            }
        } else if (selectorOrOptions && typeof selectorOrOptions === "object") {
            const { prompt: p, value: v, timeout: t, ...rest } = selectorOrOptions;
            prompt = p;
            selectValue = v;
            timeout = t;
            selectOptions = rest;
        }

        if (!selector && !prompt) {
            throw new Error("Missing input: pass a selector and/or a prompt.");
        }

        // Try to select the option with the original selector first
        let errorToRaise: Error | undefined;
        if (selector && selectValue !== undefined) {
            try {
                await this._page.selectOption(selector, selectValue, { ...selectOptions, timeout });
                return;
            } catch (error) {
                errorToRaise = error as Error;
                selector = undefined;
            }
        }

        // If the original selector doesn't work, try to select the option with AI
        if (prompt) {
            await this._ai.aiSelectOption({
                value: typeof selectValue === "string" ? selectValue : selectValue?.[0],
                intention: prompt,
                data: Object.keys(selectOptions).length > 0 ? selectOptions : undefined,
                timeout,
            });
            return;
        }

        if (errorToRaise) {
            throw errorToRaise;
        }
    }

    /**
     * Perform an action on the page using AI based on a natural language prompt.
     *
     * @param prompt - Natural language description of the action to perform.
     *
     * @example
     * ```typescript
     * // Simple action
     * await page.act("Click the login button");
     * ```
     */
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
