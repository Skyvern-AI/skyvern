import type { Page } from "playwright";
import type * as Skyvern from "../api/index.js";
import type { SkyvernBrowser } from "./SkyvernBrowser.js";
import { LOG } from "./logger.js";

export class SkyvernBrowserPageAi {
    private readonly _browser: SkyvernBrowser;
    private readonly _page: Page;

    constructor(browser: SkyvernBrowser, page: Page) {
        this._browser = browser;
        this._page = page;
    }

    /**
     * Click an element using AI via API call.
     */
    async aiClick(options: {
        selector?: string;
        intention: string;
        data?: string | Record<string, unknown>;
        timeout?: number;
    }): Promise<string | null> {
        LOG.info("AI click", { intention: options.intention, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "ai_click",
                selector: options.selector,
                intention: options.intention,
                data: options.data,
                timeout: options.timeout,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return response.result ? String(response.result) : options.selector || null;
    }

    /**
     * Input text into an element using AI via API call.
     */
    async aiInputText(options: {
        selector?: string;
        value?: string;
        intention: string;
        data?: string | Record<string, unknown>;
        totpIdentifier?: string;
        totpUrl?: string;
        timeout?: number;
    }): Promise<string> {
        LOG.info("AI input text", { intention: options.intention, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "ai_input_text",
                selector: options.selector,
                value: options.value,
                intention: options.intention,
                data: options.data,
                totp_identifier: options.totpIdentifier,
                totp_url: options.totpUrl,
                timeout: options.timeout,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return response.result ? String(response.result) : options.value || "";
    }

    /**
     * Select an option from a dropdown using AI via API call.
     */
    async aiSelectOption(options: {
        selector?: string;
        value?: string;
        intention: string;
        data?: string | Record<string, unknown>;
        timeout?: number;
    }): Promise<string> {
        LOG.info("AI select option", { intention: options.intention, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "ai_select_option",
                selector: options.selector,
                value: options.value,
                intention: options.intention,
                data: options.data,
                timeout: options.timeout,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return response.result ? String(response.result) : options.value || "";
    }

    /**
     * Upload a file using AI via API call.
     */
    async aiUploadFile(options: {
        selector?: string;
        fileUrl?: string;
        intention: string;
        data?: string | Record<string, unknown>;
        timeout?: number;
    }): Promise<string> {
        LOG.info("AI upload file", { intention: options.intention, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "ai_upload_file",
                selector: options.selector,
                file_url: options.fileUrl,
                intention: options.intention,
                data: options.data,
                timeout: options.timeout,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return response.result ? String(response.result) : options.fileUrl || "";
    }

    /**
     * Extract information from the page using AI via API call.
     */
    async aiExtract(options: {
        prompt: string;
        extractSchema?: Record<string, unknown> | unknown[] | string;
        errorCodeMapping?: Record<string, string>;
        intention?: string;
        data?: string | Record<string, unknown>;
    }): Promise<Record<string, unknown> | unknown[] | string | null> {
        LOG.info("AI extract", { prompt: options.prompt, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "extract",
                prompt: options.prompt,
                extract_schema: options.extractSchema,
                error_code_mapping: options.errorCodeMapping,
                intention: options.intention,
                data: options.data,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return (response.result as Record<string, unknown> | unknown[] | string) || null;
    }

    /**
     * Validate the current page state using AI via API call.
     */
    async aiValidate(options: { prompt: string; model?: Record<string, unknown> }): Promise<boolean> {
        LOG.info("AI validate", { prompt: options.prompt, model: options.model, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "validate",
                prompt: options.prompt,
                model: options.model,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return response.result != null ? Boolean(response.result) : false;
    }

    /**
     * Perform an action on the page using AI via API call.
     */
    async aiAct(prompt: string): Promise<void> {
        LOG.info("AI act", { prompt, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "ai_act",
                intention: prompt,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }
    }

    /**
     * Locate an element on the page using AI and return its XPath selector via API call.
     *
     * @param prompt - Natural language description of the element to locate (e.g., 'find "download invoices" button')
     *
     * @returns XPath selector string (e.g., 'xpath=//button[@id="download"]') or null if not found
     */
    async aiLocateElement(prompt: string): Promise<string | null> {
        LOG.info("AI locate element", { prompt, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "locate_element",
                prompt,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        if (response.result && typeof response.result === "string") {
            return response.result;
        }

        return null;
    }

    /**
     * Send a prompt to the LLM and get a response based on the provided schema via API call.
     */
    async aiPrompt(options: {
        prompt: string;
        schema?: Record<string, unknown>;
        model?: Record<string, unknown>;
    }): Promise<Record<string, unknown> | unknown[] | string | null> {
        LOG.info("AI prompt", { prompt: options.prompt, model: options.model, workflow_run_id: this._browser.workflowRunId });

        const response = await this._browser.skyvern.runSdkAction({
            url: this._page.url(),
            browser_session_id: this._browser.browserSessionId,
            browser_address: this._browser.browserAddress,
            workflow_run_id: this._browser.workflowRunId,
            action: {
                type: "prompt",
                prompt: options.prompt,
                schema: options.schema,
                model: options.model,
            } as Skyvern.RunSdkActionRequestAction,
        });

        if (response.workflow_run_id) {
            this._browser.workflowRunId = response.workflow_run_id;
        }

        return (response.result as Record<string, unknown> | unknown[] | string) || null;
    }
}
