import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "../types/workflowTypes";

export function apiWorkflowToSettings(
  workflow: WorkflowApiResponse,
): WorkflowSettings {
  return {
    totpVerificationUrl: workflow.totp_verification_url ?? null,
    totpIdentifier: workflow.totp_identifier ?? null,
    adaptiveCaching: workflow.adaptive_caching ?? false,
    generateScriptOnTerminal: workflow.generate_script_on_terminal ?? false,
    persistBrowserSession: workflow.persist_browser_session,
    reuseBrowserSession: workflow.reuse_browser_session ?? false,
    pinSavedSessionIp: workflow.pin_saved_session_ip ?? false,
    browserProfileId: workflow.browser_profile_id ?? null,
    browserProfileKey: workflow.browser_profile_key ?? null,
    proxyLocation: workflow.proxy_location,
    webhookCallbackUrl: workflow.webhook_callback_url,
    model: workflow.model,
    maxScreenshotScrolls: workflow.max_screenshot_scrolls,
    maxElapsedTimeMinutes: workflow.max_elapsed_time_minutes ?? null,
    extraHttpHeaders: workflow.extra_http_headers
      ? JSON.stringify(workflow.extra_http_headers)
      : null,
    cdpConnectHeaders: workflow.cdp_connect_headers
      ? JSON.stringify(workflow.cdp_connect_headers)
      : null,
    runWith: workflow.run_with ?? "agent",
    browserType: workflow.browser_type ?? null,
    codeVersion: workflow.code_version ?? null,
    scriptCacheKey: workflow.cache_key || "default",
    aiFallback: workflow.ai_fallback ?? true,
    maskSecrets: workflow.mask_secrets ?? false,
    runSequentially: workflow.run_sequentially ?? false,
    sequentialKey: workflow.sequential_key ?? null,
    finallyBlockLabel:
      workflow.workflow_definition?.finally_block_label ?? null,
    workflowSystemPrompt:
      workflow.workflow_definition?.workflow_system_prompt ?? null,
    errorCodeMapping: workflow.workflow_definition?.error_code_mapping ?? null,
    retryPolicy: workflow.workflow_definition?.retry_policy ?? null,
  };
}
