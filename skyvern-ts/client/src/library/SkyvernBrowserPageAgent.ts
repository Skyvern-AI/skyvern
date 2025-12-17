import type { Page } from "playwright";
import type * as Skyvern from "../api/index.js";
import { SkyvernEnvironment } from "../environments.js";
import { DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT } from "./constants.js";
import type { SkyvernBrowser } from "./SkyvernBrowser.js";
import { LOG } from "./logger.js";

function getAppUrlForRun(runId: string): string {
    return `https://app.skyvern.com/runs/${runId}`;
}

/**
 * Provides methods to run Skyvern tasks and workflows in the context of a browser page.
 *
 * This class enables executing AI-powered browser automation tasks while sharing the
 * context of an existing browser page. It supports running custom tasks, login workflows,
 * and pre-defined workflows with automatic waiting for completion.
 */
export class SkyvernBrowserPageAgent {
    private readonly _browser: SkyvernBrowser;
    private readonly _page: Page;

    constructor(browser: SkyvernBrowser, page: Page) {
        this._browser = browser;
        this._page = page;
    }

    /**
     * Run a task in the context of this page and wait for it to finish.
     *
     * @param prompt - Natural language description of the task to perform.
     * @param options - Optional configuration
     * @param options.engine - The execution engine to use. Defaults to skyvern_v2.
     * @param options.model - LLM model configuration options.
     * @param options.url - URL to navigate to. If not provided, uses the current page URL.
     * @param options.webhookUrl - URL to receive webhook notifications about task progress.
     * @param options.totpIdentifier - Identifier for TOTP (Time-based One-Time Password) authentication.
     * @param options.totpUrl - URL to fetch TOTP codes from.
     * @param options.title - Human-readable title for this task run.
     * @param options.errorCodeMapping - Mapping of error codes to custom error messages.
     * @param options.dataExtractionSchema - Schema defining what data to extract from the page.
     * @param options.maxSteps - Maximum number of steps the agent can take.
     * @param options.timeout - Maximum time in seconds to wait for task completion.
     *
     * @returns TaskRunResponse containing the task execution results.
     */
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

    /**
     * Run a login task in the context of this page and wait for it to finish.
     *
     * This method has multiple overloaded signatures for different credential types:
     *
     * 1. Skyvern credentials:
     *    ```typescript
     *    await page.agent.login("skyvern", {
     *        credentialId: "cred_123"
     *    });
     *    ```
     *
     * 2. Bitwarden credentials:
     *    ```typescript
     *    await page.agent.login("bitwarden", {
     *        bitwardenItemId: "item_id",
     *        bitwardenCollectionId: "collection_id"
     *    });
     *    ```
     *
     * 3. 1Password credentials:
     *    ```typescript
     *    await page.agent.login("1password", {
     *        onepasswordVaultId: "vault_id",
     *        onepasswordItemId: "item_id"
     *    });
     *    ```
     *
     * 4. Azure Vault credentials:
     *    ```typescript
     *    await page.agent.login("azure_vault", {
     *        azureVaultName: "vault_name",
     *        azureVaultUsernameKey: "username_key",
     *        azureVaultPasswordKey: "password_key",
     *    });
     *    ```
     */
    async login(
        credentialType: "skyvern",
        options: {
            credentialId: string;
            url?: string;
            prompt?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse>;
    async login(
        credentialType: "bitwarden",
        options: {
            bitwardenItemId: string;
            bitwardenCollectionId?: string;
            url?: string;
            prompt?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse>;
    async login(
        credentialType: "1password",
        options: {
            onepasswordVaultId: string;
            onepasswordItemId: string;
            url?: string;
            prompt?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse>;
    async login(
        credentialType: "azure_vault",
        options: {
            azureVaultName: string;
            azureVaultUsernameKey: string;
            azureVaultPasswordKey: string;
            azureVaultTotpSecretKey?: string;
            url?: string;
            prompt?: string;
            webhookUrl?: string;
            totpIdentifier?: string;
            totpUrl?: string;
            extraHttpHeaders?: Record<string, string>;
            timeout?: number;
        },
    ): Promise<Skyvern.WorkflowRunResponse>;
    async login(
        credentialType: Skyvern.SkyvernSchemasRunBlocksCredentialType,
        options: {
            url?: string;
            credentialId?: string;
            bitwardenCollectionId?: string;
            bitwardenItemId?: string;
            onepasswordVaultId?: string;
            onepasswordItemId?: string;
            azureVaultName?: string;
            azureVaultUsernameKey?: string;
            azureVaultPasswordKey?: string;
            azureVaultTotpSecretKey?: string;
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
                credential_type: credentialType,
                url: options.url ?? this._getPageUrl(),
                credential_id: options.credentialId,
                bitwarden_collection_id: options.bitwardenCollectionId,
                bitwarden_item_id: options.bitwardenItemId,
                onepassword_vault_id: options.onepasswordVaultId,
                onepassword_item_id: options.onepasswordItemId,
                azure_vault_name: options.azureVaultName,
                azure_vault_username_key: options.azureVaultUsernameKey,
                azure_vault_password_key: options.azureVaultPasswordKey,
                azure_vault_totp_secret_key: options.azureVaultTotpSecretKey,
                prompt: options.prompt,
                webhook_url: options.webhookUrl,
                totp_identifier: options.totpIdentifier,
                totp_url: options.totpUrl,
                browser_session_id: this._browser.browserSessionId,
                browser_address: this._browser.browserAddress,
                extra_http_headers: options.extraHttpHeaders,
            },
            {
                headers: { "x-user-agent": "skyvern-sdk" },
            },
        );

        if (this._browser.skyvern.environment === SkyvernEnvironment.Cloud) {
            LOG.info("AI login workflow is running, this may take a while", {
                url: getAppUrlForRun(workflowRun.run_id),
                run_id: workflowRun.run_id,
            });
        } else {
            LOG.info("AI login workflow is running, this may take a while", { run_id: workflowRun.run_id });
        }

        const completedRun = await this._waitForRunCompletion(workflowRun.run_id, options.timeout ?? DEFAULT_AGENT_TIMEOUT);
        LOG.info("AI login workflow finished", { run_id: completedRun.run_id, status: completedRun.status });

        return completedRun as Skyvern.WorkflowRunResponse;
    }

    /**
     * Run a file download task in the context of this page and wait for it to finish.
     *
     * @param prompt - Instructions for navigating to and downloading the file.
     * @param options - Optional configuration
     * @param options.url - URL to navigate to for file download. If not provided, uses the current page URL.
     * @param options.downloadSuffix - Suffix or complete filename for the downloaded file.
     * @param options.downloadTimeout - Timeout in seconds for the download operation.
     * @param options.maxStepsPerRun - Maximum number of steps to execute.
     * @param options.webhookUrl - URL to receive webhook notifications about download progress.
     * @param options.totpIdentifier - Identifier for TOTP authentication.
     * @param options.totpUrl - URL to fetch TOTP codes from.
     * @param options.extraHttpHeaders - Additional HTTP headers to include in requests.
     * @param options.timeout - Maximum time in seconds to wait for download completion.
     *
     * @returns WorkflowRunResponse containing the file download workflow execution results.
     */
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

    /**
     * Run a workflow in the context of this page and wait for it to finish.
     *
     * @param workflowId - ID of the workflow to execute.
     * @param options - Optional configuration
     * @param options.parameters - Dictionary of parameters to pass to the workflow.
     * @param options.template - Whether this is a workflow template.
     * @param options.title - Human-readable title for this workflow run.
     * @param options.webhookUrl - URL to receive webhook notifications about workflow progress.
     * @param options.totpUrl - URL to fetch TOTP codes from.
     * @param options.totpIdentifier - Identifier for TOTP authentication.
     * @param options.timeout - Maximum time in seconds to wait for workflow completion.
     *
     * @returns WorkflowRunResponse containing the workflow execution results.
     */
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
