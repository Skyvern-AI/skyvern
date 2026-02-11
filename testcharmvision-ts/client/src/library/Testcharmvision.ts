
import { chromium } from "playwright";
import type * as TestcharmvisionApi from "../api/index.js";
import type { BaseClientOptions } from "../BaseClient.js";
import { TestcharmvisionClient } from "../Client.js";
import { TestcharmvisionEnvironment } from "../environments.js";
import { TestcharmvisionBrowser } from "./TestcharmvisionBrowser.js";
import type { GetRunResponse, ProxyLocation } from "../api/index.js";
import { LOG } from "./logger.js";
import * as core from "../core/index.js";

function _getBrowserSessionUrl(browserSessionId: string): string {
    return `https://app.testcharmvision.com/browser-session/${browserSessionId}`;
}

export interface TestcharmvisionOptions extends BaseClientOptions {
    apiKey: string;
}

export interface RunTaskOptions extends TestcharmvisionApi.RunTaskRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface RunWorkflowOptions extends TestcharmvisionApi.RunWorkflowRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface LoginOptions extends TestcharmvisionApi.LoginRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface DownloadFilesOptions extends TestcharmvisionApi.DownloadFilesRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

/**
 * Main entry point for the Testcharmvision SDK.
 *
 * This class provides methods to launch and connect to browsers (both local and cloud-hosted),
 * and access the Testcharmvision API client for task and workflow management. It combines browser
 * automation capabilities with AI-powered task execution.
 *
 * @example
 * ```typescript
 * // Remote mode: Connect to Testcharmvision Cloud (API key required)
 * const testcharmvision = new Testcharmvision({ apiKey: "your-api-key" });
 *
 * // Launch a cloud browser
 * const browser = await testcharmvision.launchCloudBrowser();
 * const page = await browser.getWorkingPage();
 *
 * // Execute AI-powered tasks
 * await page.agent.runTask("Fill out the form and submit it");
 * ```
 */
export class Testcharmvision extends TestcharmvisionClient {
    private readonly _apiKey: string;
    private readonly _environment: TestcharmvisionEnvironment | string;
    private readonly _browsers: Set<TestcharmvisionBrowser> = new Set();

    constructor(options: TestcharmvisionOptions) {
        super({
            ...options,
            environment: options.environment ?? TestcharmvisionEnvironment.Cloud,
        });

        this._apiKey = options.apiKey;
        this._environment = (options.environment ?? TestcharmvisionEnvironment.Cloud) as TestcharmvisionEnvironment | string;
    }

    get environment(): TestcharmvisionEnvironment | string {
        return this._environment;
    }

    runTask(
        request: RunTaskOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): core.HttpResponsePromise<TestcharmvisionApi.TaskRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__runTaskWithCompletion(request, requestOptions));
    }

    private async __runTaskWithCompletion(
        request: RunTaskOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): Promise<core.WithRawResponse<TestcharmvisionApi.TaskRunResponse>> {
        const { waitForCompletion, timeout, ...taskRequest } = request;

        const response = await super.runTask(taskRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as TestcharmvisionApi.TaskRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    runWorkflow(
        request: RunWorkflowOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): core.HttpResponsePromise<TestcharmvisionApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__runWorkflowWithCompletion(request, requestOptions));
    }

    private async __runWorkflowWithCompletion(
        request: RunWorkflowOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): Promise<core.WithRawResponse<TestcharmvisionApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...workflowRequest } = request;

        const response = await super.runWorkflow(workflowRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as TestcharmvisionApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    login(
        request: LoginOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): core.HttpResponsePromise<TestcharmvisionApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__loginWithCompletion(request, requestOptions));
    }

    private async __loginWithCompletion(
        request: LoginOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): Promise<core.WithRawResponse<TestcharmvisionApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...loginRequest } = request;

        const response = await super.login(loginRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as TestcharmvisionApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    downloadFiles(
        request: DownloadFilesOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): core.HttpResponsePromise<TestcharmvisionApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__downloadFilesWithCompletion(request, requestOptions));
    }

    private async __downloadFilesWithCompletion(
        request: DownloadFilesOptions,
        requestOptions?: TestcharmvisionClient.RequestOptions,
    ): Promise<core.WithRawResponse<TestcharmvisionApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...downloadFilesRequest } = request;

        const response = await super.downloadFiles(downloadFilesRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as TestcharmvisionApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    /**
     * Launch a new cloud-hosted browser session.
     *
     * This creates a new browser session in Testcharmvision's cloud infrastructure and connects to it.
     *
     * @param options - Optional configuration
     * @param options.timeout - Timeout in minutes for the session. Timeout is applied after the session is started.
     *        Must be between 5 and 1440. Defaults to 60.
     * @param options.proxyLocation - Geographic proxy location to route the browser traffic through.
     *        This is only available in Testcharmvision Cloud.
     *
     * @returns TestcharmvisionBrowser instance connected to the new cloud session.
     */
    async launchCloudBrowser(options?: {
        timeout?: number;
        proxyLocation?: TestcharmvisionApi.ProxyLocation;
    }): Promise<TestcharmvisionBrowser> {
        this._ensureCloudEnvironment();

        const browserSession = await this.createBrowserSession({
            timeout: options?.timeout,
            proxy_location: options?.proxyLocation,
        });

        if (this._environment === TestcharmvisionEnvironment.Cloud) {
            LOG.info("Launched new cloud browser session", { url: _getBrowserSessionUrl(browserSession.browser_session_id) });
        } else {
            LOG.info("Launched new cloud browser session", { browser_session_id: browserSession.browser_session_id });
        }

        return this._connectToCloudBrowserSession(browserSession);
    }

    /**
     * Connect to an existing cloud-hosted browser session by ID.
     *
     * @param browserSessionId - The ID of the cloud browser session to connect to.
     *
     * @returns TestcharmvisionBrowser instance connected to the cloud session.
     */
    async connectToCloudBrowserSession(browserSessionId: string): Promise<TestcharmvisionBrowser> {
        this._ensureCloudEnvironment();

        const browserSession = await this.getBrowserSession(browserSessionId);

        if (this._environment === TestcharmvisionEnvironment.Cloud) {
            LOG.info("Connecting to existing cloud browser session", { url: _getBrowserSessionUrl(browserSession.browser_session_id) });
        } else {
            LOG.info("Connecting to existing cloud browser session", { browser_session_id: browserSession.browser_session_id });
        }

        return this._connectToCloudBrowserSession(browserSession);
    }

    /**
     * Get or create a cloud browser session.
     *
     * This method attempts to reuse the most recent available cloud browser session.
     * If no session exists, it creates a new one. This is useful for cost efficiency
     * and session persistence.
     *
     * @param options - Optional configuration
     * @param options.timeout - Timeout in minutes for the session. Timeout is applied after the session is started.
     *        Must be between 5 and 1440. Defaults to 60. Only used when creating a new session.
     * @param options.proxyLocation - Geographic proxy location to route the browser traffic through.
     *        This is only available in Testcharmvision Cloud. Only used when creating a new session.
     *
     * @returns TestcharmvisionBrowser instance connected to an existing or new cloud session.
     */
    async useCloudBrowser(options?: { timeout?: number; proxyLocation?: ProxyLocation }): Promise<TestcharmvisionBrowser> {
        this._ensureCloudEnvironment();

        const browserSessions = await this.getBrowserSessions();
        const browserSession = browserSessions
            .filter((s) => s.runnable_id == null && s.started_at != null && s.browser_address != null)
            .sort((a, b) => {
                const aTime = new Date(a.started_at!).getTime();
                const bTime = new Date(b.started_at!).getTime();
                return bTime - aTime;
            })
            .at(0);

        if (!browserSession) {
            LOG.info("No existing cloud browser session found, launching a new session");
            return this.launchCloudBrowser(options);
        }

        if (this._environment === TestcharmvisionEnvironment.Cloud) {
            LOG.info("Reusing existing cloud browser session", { url: _getBrowserSessionUrl(browserSession.browser_session_id) });
        } else {
            LOG.info("Reusing existing cloud browser session", { browser_session_id: browserSession.browser_session_id });
        }

        return this._connectToCloudBrowserSession(browserSession);
    }

    /**
     * Connect to an existing browser instance via Chrome DevTools Protocol (CDP).
     *
     * Use this to connect to a browser that's already running with CDP enabled,
     * whether local or remote.
     *
     * @param cdpUrl - The CDP WebSocket URL (e.g., "http://localhost:9222").
     *
     * @returns TestcharmvisionBrowser instance connected to the existing browser.
     */
    async connectToBrowserOverCdp(cdpUrl: string): Promise<TestcharmvisionBrowser> {
        const browser = await chromium.connectOverCDP(cdpUrl);
        const browserContext = browser.contexts()[0] ?? (await browser.newContext());

        const testcharmvisionBrowser = new TestcharmvisionBrowser(this, browserContext, { browser, browserAddress: cdpUrl });
        this._browsers.add(testcharmvisionBrowser);
        return testcharmvisionBrowser;
    }

    /**
     * Close all browsers and release resources.
     */
    async close(): Promise<void> {
        await Promise.all(Array.from(this._browsers).map((browser) => browser.close()));
        this._browsers.clear();
    }

    _untrackBrowser(browser: TestcharmvisionBrowser): void {
        this._browsers.delete(browser);
    }

    private _ensureCloudEnvironment(): void {
        if (this._environment !== TestcharmvisionEnvironment.Cloud && this._environment !== TestcharmvisionEnvironment.Staging) {
            throw new Error("Cloud browser sessions are supported only in the cloud environment");
        }
    }

    private async _connectToCloudBrowserSession(
        browserSession: TestcharmvisionApi.BrowserSessionResponse,
    ): Promise<TestcharmvisionBrowser> {
        if (!browserSession.browser_address) {
            throw new Error(`Browser address is missing for session ${browserSession.browser_session_id}`);
        }

        const browser = await chromium.connectOverCDP(browserSession.browser_address, {
            headers: { "x-api-key": this._apiKey },
        });
        const browserContext = browser.contexts()[0] ?? (await browser.newContext());

        const testcharmvisionBrowser = new TestcharmvisionBrowser(this, browserContext, {
            browser,
            browserSessionId: browserSession.browser_session_id,
        });
        this._browsers.add(testcharmvisionBrowser);
        return testcharmvisionBrowser;
    }

    private async _waitForRunCompletion(runId: string, timeoutSeconds: number): Promise<GetRunResponse> {
        const startTime = Date.now();
        const timeoutMs = timeoutSeconds * 1000;

        while (true) {
            const run = await this.getRun(runId);

            // Check if the run is in a final state
            const status = run.status;
            if (
                status === "completed" ||
                status === "failed" ||
                status === "terminated" ||
                status === "timed_out" ||
                status === "canceled"
            ) {
                return run;
            }

            // Check timeout
            if (Date.now() - startTime >= timeoutMs) {
                throw new Error(`Timeout waiting for run ${runId} to complete after ${timeoutSeconds} seconds`);
            }

            // Wait before polling again
            await new Promise((resolve) => setTimeout(resolve, 10000)); // 10 seconds
        }
    }
}
