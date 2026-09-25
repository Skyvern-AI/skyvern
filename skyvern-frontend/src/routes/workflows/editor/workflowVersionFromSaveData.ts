import { stringify as convertToYAML } from "yaml";

import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";

import type { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import type { WorkflowDefinition } from "../types/workflowTypes";

export class YamlCommitError extends Error {
  constructor(
    public readonly field: string,
    message: string,
  ) {
    super(`${field}: ${message}`);
    this.name = "YamlCommitError";
  }
}

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
// parsed workflow_definition and the settings supplied by the caller.
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
    description: saveData.description || null,
    workflow_definition: workflowDefinition,
    proxy_location: settings.proxyLocation,
    webhook_callback_url: settings.webhookCallbackUrl,
    extra_http_headers: headers.extraHttpHeaders,
    cdp_connect_headers: headers.cdpConnectHeaders,
    persist_browser_session: settings.persistBrowserSession,
    reuse_browser_session: settings.reuseBrowserSession,
    pin_saved_session_ip: settings.pinSavedSessionIp,
    browser_profile_id: settings.browserProfileId,
    browser_profile_key: settings.browserProfileKey,
    model: settings.model,
    totp_verification_url: settings.totpVerificationUrl,
    totp_identifier: settings.totpIdentifier,
    max_screenshot_scrolls: settings.maxScreenshotScrolls,
    max_elapsed_time_minutes: settings.maxElapsedTimeMinutes ?? null,
    status: workflow.status,
    created_at: workflow.created_at,
    modified_at: workflow.modified_at,
    deleted_at: workflow.deleted_at ?? null,
    run_with: settings.runWith,
    browser_type: settings.browserType ?? null,
    cache_key: settings.scriptCacheKey,
    ai_fallback: settings.aiFallback,
    enable_self_healing: workflow.enable_self_healing,
    adaptive_caching: settings.adaptiveCaching,
    generate_script_on_terminal: settings.generateScriptOnTerminal,
    mask_secrets: settings.maskSecrets,
    code_version:
      settings.runWith === "code" ? (settings.codeVersion ?? 2) : null,
    run_sequentially: settings.runSequentially,
    sequential_key: settings.sequentialKey,
    folder_id: workflow.folder_id ?? null,
    import_error: workflow.import_error ?? null,
  };
}

const topLevelSettingKeys = [
  "webhook_callback_url",
  "proxy_location",
  "persist_browser_session",
  "reuse_browser_session",
  "pin_saved_session_ip",
  "browser_profile_id",
  "browser_profile_key",
  "model",
  "max_screenshot_scrolls",
  "max_elapsed_time_minutes",
  "extra_http_headers",
  "cdp_connect_headers",
  "run_with",
  "browser_type",
  "code_version",
  "cache_key",
  "ai_fallback",
  "mask_secrets",
  "run_sequentially",
  "sequential_key",
  "totp_verification_url",
  "totp_identifier",
  "adaptive_caching",
  "generate_script_on_terminal",
] as const;
const definitionSettingKeys = [
  "finally_block_label",
  "workflow_system_prompt",
  "error_code_mapping",
  "retry_policy",
] as const;
const definitionKeys = new Set<string>([
  ...definitionSettingKeys,
  "version",
  "parameters",
  "blocks",
]);
export type SettingsPatch = Partial<
  Record<
    | (typeof topLevelSettingKeys)[number]
    | (typeof definitionSettingKeys)[number],
    unknown
  >
>;
export type MetadataPatch = { title?: string; description?: string | null };
const managedKeys = new Set([
  "is_saved_task",
  "status",
  "folder_id",
  "workflow_id",
  "organization_id",
  "workflow_permanent_id",
  "version",
  "created_at",
  "modified_at",
  "deleted_at",
  "import_error",
  "created_by",
  "edited_by",
  "copilot_authored",
  "is_template",
  "enable_self_healing",
]);

export function isPlainMapping(
  value: unknown,
): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    return false;
  const prototype: unknown = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function yamlCommitInputs<T>(
  parsed: T,
  draft: string,
): {
  kind: "legacy" | "envelope";
  definition: T;
  definitionYaml: string;
  settingsPatch: SettingsPatch;
  metadataPatch: MetadataPatch;
} {
  if (!isPlainMapping(parsed)) {
    throw new YamlCommitError("document", "document root must be a mapping");
  }
  const has = (key: string) =>
    Object.prototype.hasOwnProperty.call(parsed, key);
  const envelopeKeys = [...topLevelSettingKeys, "title", "description"];
  const envelope = has("workflow_definition") || envelopeKeys.some(has);
  if (has("blocks") && has("workflow_definition")) {
    throw new YamlCommitError("document", "ambiguous document");
  }
  if (!envelope) {
    if (!has("blocks")) {
      throw new YamlCommitError(
        "document",
        "document must contain blocks or workflow_definition",
      );
    }
    return {
      kind: "legacy",
      definition: parsed,
      definitionYaml: draft,
      settingsPatch: {},
      metadataPatch: {},
    };
  }
  if (!has("workflow_definition")) {
    throw new YamlCommitError(
      "workflow_definition",
      "workflow_definition is required in a full document",
    );
  }
  const definition = parsed.workflow_definition;
  if (!isPlainMapping(definition) || !Array.isArray(definition.blocks)) {
    throw new YamlCommitError(
      "workflow_definition",
      "workflow_definition must be a mapping with a blocks list",
    );
  }
  const unknownKeys = Object.keys(parsed).filter(
    (key) =>
      key !== "workflow_definition" &&
      !envelopeKeys.includes(key) &&
      !managedKeys.has(key),
  );
  if (unknownKeys.length) {
    throw new YamlCommitError(unknownKeys.join(", "), "unknown top-level keys");
  }
  for (const key of Object.keys(definition)) {
    if (!definitionKeys.has(key)) {
      throw new YamlCommitError(
        `workflow_definition.${key}`,
        "unknown setting",
      );
    }
  }
  const settingsPatch: SettingsPatch = {};
  for (const key of topLevelSettingKeys) {
    if (has(key)) settingsPatch[key] = parsed[key];
  }
  for (const key of definitionSettingKeys) {
    if (Object.prototype.hasOwnProperty.call(definition, key))
      settingsPatch[key] = definition[key];
  }
  const metadataPatch: MetadataPatch = {};
  if (has("title")) {
    if (typeof parsed.title !== "string" || !parsed.title.trim()) {
      throw new YamlCommitError("title", "must be a non-empty string");
    }
    metadataPatch.title = parsed.title;
  }
  if (has("description")) {
    if (parsed.description !== null && typeof parsed.description !== "string") {
      throw new YamlCommitError("description", "must be a string or null");
    }
    metadataPatch.description = parsed.description || null;
  }
  return {
    kind: "envelope",
    definition: definition as T,
    definitionYaml: convertToYAML(definition),
    settingsPatch,
    metadataPatch,
  };
}
