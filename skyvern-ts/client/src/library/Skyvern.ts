
import { chromium } from "playwright";
import type * as SkyvernApi from "../api/index.js";
import type { BaseClientOptions } from "../BaseClient.js";
import { SkyvernClient } from "../Client.js";
import { SkyvernEnvironment } from "../environments.js";
import { SkyvernBrowser } from "./SkyvernBrowser.js";
import type { GetRunResponse, ProxyLocation } from "../api/index.js";
import { LOG } from "./logger.js";
import * as core from "../core/index.js";

export interface SkyvernOptions extends BaseClientOptions {
    apiKey: string;
}

export interface RunTaskOptions extends SkyvernApi.RunTaskRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface RunWorkflowOptions extends SkyvernApi.RunWorkflowRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface LoginOptions extends SkyvernApi.LoginRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export interface DownloadFilesOptions extends SkyvernApi.DownloadFilesRequest {
    waitForCompletion?: boolean;
    timeout?: number;
}

export class Skyvern extends SkyvernClient {
    private readonly _apiKey: string;
    private readonly _environment: SkyvernEnvironment | string;
    private readonly _browsers: Set<SkyvernBrowser> = new Set();

    constructor(options: SkyvernOptions) {
        super({
            ...options,
            environment: options.environment ?? SkyvernEnvironment.Cloud,
        });

        this._apiKey = options.apiKey;
        this._environment = (options.environment ?? SkyvernEnvironment.Cloud) as SkyvernEnvironment | string;
    }

    get environment(): SkyvernEnvironment | string {
        return this._environment;
    }

    runTask(
        request: RunTaskOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): core.HttpResponsePromise<SkyvernApi.TaskRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__runTaskWithCompletion(request, requestOptions));
    }

    private async __runTaskWithCompletion(
        request: RunTaskOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): Promise<core.WithRawResponse<SkyvernApi.TaskRunResponse>> {
        const { waitForCompletion, timeout, ...taskRequest } = request;

        const response = await super.runTask(taskRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as SkyvernApi.TaskRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    runWorkflow(
        request: RunWorkflowOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): core.HttpResponsePromise<SkyvernApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__runWorkflowWithCompletion(request, requestOptions));
    }

    private async __runWorkflowWithCompletion(
        request: RunWorkflowOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): Promise<core.WithRawResponse<SkyvernApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...workflowRequest } = request;

        const response = await super.runWorkflow(workflowRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as SkyvernApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    login(
        request: LoginOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): core.HttpResponsePromise<SkyvernApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__loginWithCompletion(request, requestOptions));
    }

    private async __loginWithCompletion(
        request: LoginOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): Promise<core.WithRawResponse<SkyvernApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...loginRequest } = request;

        const response = await super.login(loginRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as SkyvernApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    downloadFiles(
        request: DownloadFilesOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): core.HttpResponsePromise<SkyvernApi.WorkflowRunResponse> {
        return core.HttpResponsePromise.fromPromise(this.__downloadFilesWithCompletion(request, requestOptions));
    }

    private async __downloadFilesWithCompletion(
        request: DownloadFilesOptions,
        requestOptions?: SkyvernClient.RequestOptions,
    ): Promise<core.WithRawResponse<SkyvernApi.WorkflowRunResponse>> {
        const { waitForCompletion, timeout, ...downloadFilesRequest } = request;

        const response = await super.downloadFiles(downloadFilesRequest, requestOptions).withRawResponse();

        if (waitForCompletion) {
            const completedRun = await this._waitForRunCompletion(
                response.data.run_id,
                timeout ?? 1800,
            ) as SkyvernApi.WorkflowRunResponse;
            return { data: completedRun, rawResponse: response.rawResponse };
        }

        return response;
    }

    async launchCloudBrowser(options?: {
        timeout?: number;
        proxyLocation?: SkyvernApi.ProxyLocation;
    }): Promise<SkyvernBrowser> {
        this._ensureCloudEnvironment();

        const browserSession = await this.createBrowserSession({
            timeout: options?.timeout,
            proxy_location: options?.proxyLocation,
        });

        LOG.info("Launched new cloud browser session", { browser_session_id: browserSession.browser_session_id });

        return this._connectToCloudBrowserSession(browserSession);
    }

    async connectToCloudBrowserSession(browserSessionId: string): Promise<SkyvernBrowser> {
        this._ensureCloudEnvironment();

        const browserSession = await this.getBrowserSession(browserSessionId);

        LOG.info("Connecting to existing cloud browser session", { browser_session_id: browserSession.browser_session_id });

        return this._connectToCloudBrowserSession(browserSession);
    }

    async useCloudBrowser(options?: { timeout?: number; proxyLocation?: ProxyLocation }): Promise<SkyvernBrowser> {
        this._ensureCloudEnvironment();

        const browserSessions = await this.getBrowserSessions();
        const browserSession = browserSessions
            .filter((s) => s.runnable_id == null)
            .sort((a, b) => {
                const aTime = a.started_at ? new Date(a.started_at).getTime() : 0;
                const bTime = b.started_at ? new Date(b.started_at).getTime() : 0;
                return bTime - aTime;
            })[0];

        if (!browserSession) {
            LOG.info("No existing cloud browser session found, launching a new session");
            return this.launchCloudBrowser(options);
        }

        LOG.info("Reusing existing cloud browser session", { browser_session_id: browserSession.browser_session_id });

        return this._connectToCloudBrowserSession(browserSession);
    }

    async connectToBrowserOverCdp(cdpUrl: string): Promise<SkyvernBrowser> {
        const browser = await chromium.connectOverCDP(cdpUrl);
        const browserContext = browser.contexts()[0] ?? (await browser.newContext());

        const skyvernBrowser = new SkyvernBrowser(this, browserContext, { browser, browserAddress: cdpUrl });
        this._browsers.add(skyvernBrowser);
        return skyvernBrowser;
    }

    async close(): Promise<void> {
        await Promise.all(Array.from(this._browsers).map((browser) => browser.close()));
        this._browsers.clear();
    }

    _untrackBrowser(browser: SkyvernBrowser): void {
        this._browsers.delete(browser);
    }

    private _ensureCloudEnvironment(): void {
        if (this._environment !== SkyvernEnvironment.Cloud && this._environment !== SkyvernEnvironment.Staging) {
            throw new Error("Cloud browser sessions are supported only in the cloud environment");
        }
    }

    private async _connectToCloudBrowserSession(
        browserSession: SkyvernApi.BrowserSessionResponse,
    ): Promise<SkyvernBrowser> {
        if (!browserSession.browser_address) {
            throw new Error(`Browser address is missing for session ${browserSession.browser_session_id}`);
        }

        const browser = await chromium.connectOverCDP(browserSession.browser_address, {
            headers: { "x-api-key": this._apiKey },
        });
        const browserContext = browser.contexts()[0] ?? (await browser.newContext());

        const skyvernBrowser = new SkyvernBrowser(this, browserContext, {
            browser,
            browserSessionId: browserSession.browser_session_id,
        });
        this._browsers.add(skyvernBrowser);
        return skyvernBrowser;
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
