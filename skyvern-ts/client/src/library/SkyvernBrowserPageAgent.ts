import type { Page } from "playwright";
import type * as Skyvern from "../api/index.js";
import { SkyvernEnvironment } from "../environments.js";
import { DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT } from "./constants.js";
import type { SkyvernBrowser } from "./SkyvernBrowser.js";
import { LOG } from "./logger.js";

function getAppUrlForRun(runId: string): string {
    return `https://app.skyvern.com/runs/${runId}`;
}

export class SkyvernBrowserPageAgent {
    private readonly _browser: SkyvernBrowser;
    private readonly _page: Page;

    constructor(browser: SkyvernBrowser, page: Page) {
        this._browser = browser;
        this._page = page;
    }

    async runTask(
        prompt: string,
        options?: {
            engine?: Skyvern.RunEngine;
            model?: Record<string, unknown>;
            url?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            title?: string;
            errorCodeMapping?: Record<string, string>;
            dataExtractionSchema?: Record<string, unknown> | string;
            maxSteps?: number;
            timeout?: number;
        },
    ): Promise<Skyvern.TaskRunResponse> {
        LOG.info("AI run task", { prompt });

        const taskRun = await this._browser.skyvern.runTask({
            "x-user-agent": "skyvern-sdk",
            body: {
                prompt: prompt,
                engine: options?.engine,
                model: options?.model,
                url: options?.url ?? this._getPageUrl(),
                webhook_url: options?.webhookUrl,
                totp_identifier: options?.totpIdentifier,
                totp_url: options?.totpUrl,
                title: options?.title,
                error_code_mapping: options?.errorCodeMapping,
                data_extraction_schema: options?.dataExtractionSchema,
                max_steps: options?.maxSteps,
                browser_session_id: this._browser.browserSessionId,
                browser_address: this._browser.browserAddress,
            },
        });

        if (this._browser.skyvern.environment === SkyvernEnvironment.Cloud) {
            LOG.info("AI task is running, this may take a while", { url: getAppUrlForRun(taskRun.run_id), run_id: taskRun.run_id });
        } else {
            LOG.info("AI task is running, this may take a while", { run_id: taskRun.run_id });
        }

        const completedRun = await this._waitForRunCompletion(
            taskRun.run_id,
            options?.timeout ?? DEFAULT_AGENT_TIMEOUT,
        );
        LOG.info("AI task finished", { run_id: completedRun.run_id, status: completedRun.status });

        return completedRun as Skyvern.TaskRunResponse;
    }

    async login(
        credentialType: string,
        options?: {
            url?: string;
            credentialId?: string;
            bitwardenCollectionId?: string;
            bitwardenItemId?: string;
            onepasswordVaultId?: string;
            onepasswordItemId?: string;
            prompt?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse> {
        LOG.info("Starting AI login workflow", { credential_type: credentialType });

        const workflowRun = await this._browser.skyvern.login(
            {
                credential_type: credentialType as Skyvern.SkyvernSchemasRunBlocksCredentialType,
                url: options?.url ?? this._getPageUrl(),
                credential_id: options?.credentialId,
                bitwarden_collection_id: options?.bitwardenCollectionId,
                bitwarden_item_id: options?.bitwardenItemId,
                onepassword_vault_id: options?.onepasswordVaultId,
                onepassword_item_id: options?.onepasswordItemId,
                prompt: options?.prompt,
                webhook_url: options?.webhookUrl,
                totp_identifier: options?.totpIdentifier,
                totp_url: options?.totpUrl,
                browser_session_id: this._browser.browserSessionId,
                browser_address: this._browser.browserAddress,
                extra_http_headers: options?.extraHttpHeaders,
            },
            {
                headers: { "x-user-agent": "skyvern-sdk" },
            },
        );

        if (this._browser.skyvern.environment === SkyvernEnvironment.Cloud) {
            LOG.info("AI login workflow is running, this may take a while", { url: getAppUrlForRun(workflowRun.run_id), run_id: workflowRun.run_id });
        } else {
            LOG.info("AI login workflow is running, this may take a while", { run_id: workflowRun.run_id });
        }

        const completedRun = await this._waitForRunCompletion(
            workflowRun.run_id,
            options?.timeout ?? DEFAULT_AGENT_TIMEOUT,
        );
        LOG.info("AI login workflow finished", { run_id: completedRun.run_id, status: completedRun.status });

        return completedRun as Skyvern.WorkflowRunResponse;
    }

    async downloadFiles(
        prompt: string,
        options?: {
            url?: string;
            downloadSuffix?: string;
            downloadTimeout?: number;
            maxStepsPerRun?: number;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse> {
        LOG.info("Starting AI file download workflow", { navigation_goal: prompt });

        const workflowRun = await this._browser.skyvern.downloadFiles(
            {
                navigation_goal: prompt,
                url: options?.url ?? this._getPageUrl(),
                download_suffix: options?.downloadSuffix,
                download_timeout: options?.downloadTimeout,
                max_steps_per_run: options?.maxStepsPerRun,
                webhook_url: options?.webhookUrl,
                totp_identifier: options?.totpIdentifier,
                totp_url: options?.totpUrl,
                browser_session_id: this._browser.browserSessionId,
                browser_address: this._browser.browserAddress,
                extra_http_headers: options?.extraHttpHeaders,
            },
            {
                headers: { "x-user-agent": "skyvern-sdk" },
            },
        );

        LOG.info("AI file download workflow is running, this may take a while", { run_id: workflowRun.run_id });

        const completedRun = await this._waitForRunCompletion(
            workflowRun.run_id,
            options?.timeout ?? DEFAULT_AGENT_TIMEOUT,
        );
        LOG.info("AI file download workflow finished", { run_id: completedRun.run_id, status: completedRun.status });

        return completedRun as Skyvern.WorkflowRunResponse;
    }

    async runWorkflow(
        workflowId: string,
        options?: {
            parameters?: Record<string, unknown>;
            template?: boolean;
            title?: string;
            webhookUrl?: string;
            totpUrl?: string;
            totpIdentifier?: string;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse> {
        LOG.info("Starting AI workflow", { workflow_id: workflowId });

        const workflowRun = await this._browser.skyvern.runWorkflow(
            {
                "x-user-agent": "skyvern-sdk",
                template: options?.template,
                body: {
                    workflow_id: workflowId,
                    parameters: options?.parameters,
                    title: options?.title,
                    webhook_url: options?.webhookUrl,
                    totp_url: options?.totpUrl,
                    totp_identifier: options?.totpIdentifier,
                    browser_session_id: this._browser.browserSessionId,
                    browser_address: this._browser.browserAddress,
                },
            },
            {
                headers: { "x-user-agent": "skyvern-sdk" },
            },
        );

        if (this._browser.skyvern.environment === SkyvernEnvironment.Cloud) {
            LOG.info("AI workflow is running, this may take a while", { url: getAppUrlForRun(workflowRun.run_id), run_id: workflowRun.run_id });
        } else {
            LOG.info("AI workflow is running, this may take a while", { run_id: workflowRun.run_id });
        }

        const completedRun = await this._waitForRunCompletion(
            workflowRun.run_id,
            options?.timeout ?? DEFAULT_AGENT_TIMEOUT,
        );
        LOG.info("AI workflow finished", { run_id: completedRun.run_id, status: completedRun.status });

        return completedRun as Skyvern.WorkflowRunResponse;
    }

    private async _waitForRunCompletion(runId: string, timeoutSeconds: number): Promise<Skyvern.GetRunResponse> {
        const startTime = Date.now();
        const timeoutMs = timeoutSeconds * 1000;

        while (true) {
            const run = await this._browser.skyvern.getRun(runId);

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
            await new Promise((resolve) => setTimeout(resolve, DEFAULT_AGENT_HEARTBEAT_INTERVAL * 1000));
        }
    }

    private _getPageUrl(): string | undefined {
        const url = this._page.url();
        if (url === "about:blank") {
            return undefined;
        }
        return url;
    }
}
