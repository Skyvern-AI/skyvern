import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";

import type { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import type { WorkflowDefinition } from "../types/workflowTypes";

// finally_block_label references a top-level block. After a YAML edit that
// removed or renamed that block, reattaching the old label would leave a
// dangling reference and the save would fail — so keep it only when the block
// still exists among the committed top-level blocks.
export function preservedFinallyBlockLabel(
  finallyBlockLabel: string | null | undefined,
  topLevelBlockLabels: Iterable<string>,
): string | null {
  if (!finallyBlockLabel) {
    return null;
  }
  return new Set(topLevelBlockLabels).has(finallyBlockLabel)
    ? finallyBlockLabel
    : null;
}

// Builds a WorkflowVersion from the live editor state, swapping in a freshly
// parsed workflow_definition — so YAML mode only ever changes the definition.
// NOTE: every WorkflowVersion field is mapped by hand below. Adding a field to
// the type without adding it here silently drops it on a YAML commit (a
// ghost-reset on next reload), with no type error to catch the omission.
export function workflowVersionFromSaveData(
  saveData: WorkflowSaveData,
  workflowDefinition: WorkflowDefinition,
  headers: {
    extraHttpHeaders: Record<string, string> | null;
    cdpConnectHeaders: Record<string, string> | null;
  },
): WorkflowVersion {
  const { settings, workflow } = saveData;
  return {
    workflow_id: workflow.workflow_id,
    organization_id: workflow.organization_id,
    is_saved_task: workflow.is_saved_task ?? false,
    is_template: workflow.is_template ?? false,
    title: saveData.title,
    workflow_permanent_id: workflow.workflow_permanent_id,
    version: workflow.version ?? 0,
    description: workflow.description ?? "",
    workflow_definition: workflowDefinition,
    proxy_location: settings.proxyLocation,
    webhook_callback_url: settings.webhookCallbackUrl,
    extra_http_headers: headers.extraHttpHeaders,
    cdp_connect_headers: headers.cdpConnectHeaders,
    persist_browser_session: settings.persistBrowserSession,
    browser_profile_id: settings.browserProfileId,
    browser_profile_key: settings.browserProfileKey,
    model: settings.model,
    totp_verification_url: workflow.totp_verification_url,
    totp_identifier: workflow.totp_identifier ?? null,
    max_screenshot_scrolls: settings.maxScreenshotScrolls,
    max_elapsed_time_minutes: settings.maxElapsedTimeMinutes ?? null,
    status: workflow.status,
    created_at: workflow.created_at,
    modified_at: workflow.modified_at,
    deleted_at: workflow.deleted_at ?? null,
    run_with: settings.runWith,
    cache_key: settings.scriptCacheKey,
    ai_fallback: settings.aiFallback,
    adaptive_caching: workflow.adaptive_caching ?? false,
    code_version:
      settings.runWith === "code" ? (settings.codeVersion ?? 2) : null,
    run_sequentially: settings.runSequentially,
    sequential_key: settings.sequentialKey,
    folder_id: workflow.folder_id ?? null,
    import_error: workflow.import_error ?? null,
  };
}
