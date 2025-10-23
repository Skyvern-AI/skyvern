import * as Skyvern from "./api/index.mjs";
import { Scripts } from "./api/resources/scripts/client/Client.mjs";
import type { BaseClientOptions, BaseRequestOptions } from "./BaseClient.mjs";
import * as core from "./core/index.mjs";
export declare namespace SkyvernClient {
    interface Options extends BaseClientOptions {
    }
    interface RequestOptions extends BaseRequestOptions {
    }
}
export declare class SkyvernClient {
    protected readonly _options: SkyvernClient.Options;
    protected _scripts: Scripts | undefined;
    constructor(_options?: SkyvernClient.Options);
    get scripts(): Scripts;
    /**
     * Run a task
     *
     * @param {Skyvern.RunTaskRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.BadRequestError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.runTask({
     *         "x-user-agent": "x-user-agent",
     *         body: {
     *             prompt: "Find the top 3 posts on Hacker News."
     *         }
     *     })
     */
    runTask(request: Skyvern.RunTaskRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.TaskRunResponse>;
    private __runTask;
    /**
     * Run a workflow
     *
     * @param {Skyvern.RunWorkflowRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.BadRequestError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.runWorkflow({
     *         "x-max-steps-override": 1,
     *         "x-user-agent": "x-user-agent",
     *         template: true,
     *         body: {
     *             workflow_id: "wpid_123"
     *         }
     *     })
     */
    runWorkflow(request: Skyvern.RunWorkflowRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.WorkflowRunResponse>;
    private __runWorkflow;
    /**
     * Get run information (task run, workflow run)
     *
     * @param {string} runId - The id of the task run or the workflow run.
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.NotFoundError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getRun("tsk_123")
     */
    getRun(runId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.GetRunResponse>;
    private __getRun;
    /**
     * Cancel a run (task or workflow)
     *
     * @param {string} runId - The id of the task run or the workflow run to cancel.
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.cancelRun("run_id")
     */
    cancelRun(runId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<unknown>;
    private __cancelRun;
    /**
     * Get all workflows with the latest version for the organization.
     *
     * Search semantics:
     * - If `search_key` is provided, its value is used as a unified search term for both
     *   `workflows.title` and workflow parameter metadata (key, description, and default_value for
     *   `WorkflowParameterModel`).
     * - Falls back to deprecated `title` (title-only search) if `search_key` is not provided.
     * - Parameter metadata search excludes soft-deleted parameter rows across all parameter tables.
     *
     * @param {Skyvern.GetWorkflowsRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getWorkflows({
     *         page: 1,
     *         page_size: 1,
     *         only_saved_tasks: true,
     *         only_workflows: true,
     *         search_key: "search_key",
     *         title: "title",
     *         template: true
     *     })
     */
    getWorkflows(request?: Skyvern.GetWorkflowsRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Workflow[]>;
    private __getWorkflows;
    /**
     * Create a new workflow
     *
     * @param {Skyvern.WorkflowRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.createWorkflow({})
     */
    createWorkflow(request: Skyvern.WorkflowRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Workflow>;
    private __createWorkflow;
    /**
     * Update a workflow
     *
     * @param {string} workflowId - The ID of the workflow to update. Workflow ID starts with `wpid_`.
     * @param {Skyvern.WorkflowRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.updateWorkflow("wpid_123", {})
     */
    updateWorkflow(workflowId: string, request: Skyvern.WorkflowRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Workflow>;
    private __updateWorkflow;
    /**
     * Delete a workflow
     *
     * @param {string} workflowId - The ID of the workflow to delete. Workflow ID starts with `wpid_`.
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.deleteWorkflow("wpid_123")
     */
    deleteWorkflow(workflowId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<unknown>;
    private __deleteWorkflow;
    /**
     * Get an artifact
     *
     * @param {string} artifactId
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.NotFoundError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getArtifact("artifact_id")
     */
    getArtifact(artifactId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Artifact>;
    private __getArtifact;
    /**
     * Get artifacts for a run
     *
     * @param {string} runId - The id of the task run or the workflow run.
     * @param {Skyvern.GetRunArtifactsRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getRunArtifacts("run_id")
     */
    getRunArtifacts(runId: string, request?: Skyvern.GetRunArtifactsRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Artifact[]>;
    private __getRunArtifacts;
    /**
     * Retry sending the webhook for a run
     *
     * @param {string} runId - The id of the task run or the workflow run.
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.retryRunWebhook("tsk_123")
     */
    retryRunWebhook(runId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<unknown>;
    private __retryRunWebhook;
    /**
     * Get timeline for a run (workflow run or task_v2 run)
     *
     * @param {string} runId - The id of the workflow run or task_v2 run.
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.BadRequestError}
     * @throws {@link Skyvern.NotFoundError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getRunTimeline("wr_123")
     */
    getRunTimeline(runId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.WorkflowRunTimeline[]>;
    private __getRunTimeline;
    /**
     * Get all active browser sessions for the organization
     *
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.ForbiddenError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getBrowserSessions()
     */
    getBrowserSessions(requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.BrowserSessionResponse[]>;
    private __getBrowserSessions;
    /**
     * Create a browser session that persists across multiple runs
     *
     * @param {Skyvern.CreateBrowserSessionRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.ForbiddenError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.createBrowserSession()
     */
    createBrowserSession(request?: Skyvern.CreateBrowserSessionRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.BrowserSessionResponse>;
    private __createBrowserSession;
    /**
     * Close a session. Once closed, the session cannot be used again.
     *
     * @param {string} browserSessionId - The ID of the browser session to close. completed_at will be set when the browser session is closed. browser_session_id starts with `pbs_`
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.ForbiddenError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.closeBrowserSession("pbs_123456")
     */
    closeBrowserSession(browserSessionId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<unknown>;
    private __closeBrowserSession;
    /**
     * Get details about a specific browser session, including the browser address for cdp connection.
     *
     * @param {string} browserSessionId - The ID of the browser session. browser_session_id starts with `pbs_`
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.ForbiddenError}
     * @throws {@link Skyvern.NotFoundError}
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getBrowserSession("pbs_123456")
     */
    getBrowserSession(browserSessionId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.BrowserSessionResponse>;
    private __getBrowserSession;
    /**
     * Forward a TOTP (2FA, MFA) email or sms message containing the code to Skyvern. This endpoint stores the code in database so that Skyvern can use it while running tasks/workflows.
     *
     * @param {Skyvern.TotpCodeCreate} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.sendTotpCode({
     *         totp_identifier: "john.doe@example.com",
     *         content: "Hello, your verification code is 123456"
     *     })
     */
    sendTotpCode(request: Skyvern.TotpCodeCreate, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.TotpCode>;
    private __sendTotpCode;
    /**
     * Retrieves a paginated list of credentials for the current organization
     *
     * @param {Skyvern.GetCredentialsRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getCredentials({
     *         page: 1,
     *         page_size: 10
     *     })
     */
    getCredentials(request?: Skyvern.GetCredentialsRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.CredentialResponse[]>;
    private __getCredentials;
    /**
     * Creates a new credential for the current organization
     *
     * @param {Skyvern.CreateCredentialRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.createCredential({
     *         name: "My Credential",
     *         credential_type: "password",
     *         credential: {
     *             password: "securepassword123",
     *             username: "user@example.com",
     *             totp: "JBSWY3DPEHPK3PXP"
     *         }
     *     })
     */
    createCredential(request: Skyvern.CreateCredentialRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.CredentialResponse>;
    private __createCredential;
    /**
     * Deletes a specific credential by its ID
     *
     * @param {string} credentialId - The unique identifier of the credential to delete
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.deleteCredential("cred_1234567890")
     */
    deleteCredential(credentialId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<void>;
    private __deleteCredential;
    /**
     * Retrieves a specific credential by its ID
     *
     * @param {string} credentialId - The unique identifier of the credential
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getCredential("cred_1234567890")
     */
    getCredential(credentialId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.CredentialResponse>;
    private __getCredential;
    /**
     * Log in to a website using either credential stored in Skyvern, Bitwarden, 1Password, or Azure Vault
     *
     * @param {Skyvern.LoginRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.login({
     *         credential_type: "skyvern"
     *     })
     */
    login(request: Skyvern.LoginRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.WorkflowRunResponse>;
    private __login;
    /**
     * Retrieves a paginated list of scripts for the current organization
     *
     * @param {Skyvern.GetScriptsRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getScripts({
     *         page: 1,
     *         page_size: 10
     *     })
     */
    getScripts(request?: Skyvern.GetScriptsRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Script[]>;
    private __getScripts;
    /**
     * Create a new script with optional files and metadata
     *
     * @param {Skyvern.CreateScriptRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.createScript()
     */
    createScript(request?: Skyvern.CreateScriptRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.CreateScriptResponse>;
    private __createScript;
    /**
     * Retrieves a specific script by its ID
     *
     * @param {string} scriptId - The unique identifier of the script
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.getScript("s_abc123")
     */
    getScript(scriptId: string, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.Script>;
    private __getScript;
    /**
     * Deploy a script with updated files, creating a new version
     *
     * @param {string} scriptId - The unique identifier of the script
     * @param {Skyvern.DeployScriptRequest} request
     * @param {SkyvernClient.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.deployScript("s_abc123", {
     *         files: [{
     *                 path: "src/main.py",
     *                 content: "content"
     *             }]
     *     })
     */
    deployScript(scriptId: string, request: Skyvern.DeployScriptRequest, requestOptions?: SkyvernClient.RequestOptions): core.HttpResponsePromise<Skyvern.CreateScriptResponse>;
    private __deployScript;
    protected _getCustomAuthorizationHeaders(): Promise<Record<string, string | undefined>>;
}
