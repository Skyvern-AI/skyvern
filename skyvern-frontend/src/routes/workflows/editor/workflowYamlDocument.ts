import type { GeoTarget } from "@/api/types";
import { SUPPORTED_COUNTRY_CODES } from "@/util/geoData";
import { validateRetryPolicyInput } from "./nodes/StartNode/retryPolicyUtils";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";
import { getJsonParseErrorDetail } from "@/util/jsonParseError";
import { apiWorkflowToSettings } from "./apiWorkflowToSettings";
import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "../types/workflowTypes";
import type {
  BlockYAML,
  ParameterYAML,
  WorkflowCreateYAMLRequest,
} from "../types/workflowYamlTypes";
import {
  isPlainMapping,
  preservedFinallyBlockLabel,
  YamlCommitError,
  type SettingsPatch,
} from "./workflowVersionFromSaveData";

function parseHeaders(
  value: string | null,
  field: string,
): Record<string, string> | null {
  if (!value) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (error) {
    throw new YamlCommitError(
      field,
      `Invalid JSON: ${getJsonParseErrorDetail(value, error)}`,
    );
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
    return null;
  return Object.fromEntries(
    Object.entries(parsed)
      .filter(([key]) => key !== "")
      .map(([key, value]) => [key, String(value)]),
  );
}

export function buildWorkflowSaveRequest(
  saveData: Omit<WorkflowSaveData, "workflow"> & {
    workflow: Pick<WorkflowApiResponse, "is_saved_task" | "status">;
  },
  opts?: { status?: string },
): WorkflowCreateYAMLRequest {
  const extraHttpHeaders =
    parseHeaders(saveData.settings.extraHttpHeaders, "extra_http_headers") ??
    {};
  const cdpConnectHeaders = parseHeaders(
    saveData.settings.cdpConnectHeaders,
    "cdp_connect_headers",
  );

  return {
    title: saveData.title,
    description: saveData.description || null,
    proxy_location: saveData.settings.proxyLocation,
    webhook_callback_url: saveData.settings.webhookCallbackUrl || null,
    persist_browser_session: saveData.settings.persistBrowserSession,
    reuse_browser_session: saveData.settings.reuseBrowserSession,
    pin_saved_session_ip: saveData.settings.pinSavedSessionIp,
    browser_profile_id: saveData.settings.browserProfileId,
    browser_profile_key: saveData.settings.browserProfileKey,
    model: saveData.settings.model,
    max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
    max_elapsed_time_minutes: saveData.settings.maxElapsedTimeMinutes ?? null,
    totp_verification_url: saveData.settings.totpVerificationUrl,
    totp_identifier: saveData.settings.totpIdentifier,
    adaptive_caching: saveData.settings.adaptiveCaching,
    generate_script_on_terminal: saveData.settings.generateScriptOnTerminal,
    extra_http_headers: extraHttpHeaders,
    cdp_connect_headers: cdpConnectHeaders ?? {},
    run_with: saveData.settings.runWith,
    browser_type: saveData.settings.browserType ?? null,
    cache_key: saveData.settings.scriptCacheKey || "default",
    ai_fallback: saveData.settings.aiFallback ?? true,
    mask_secrets: saveData.settings.maskSecrets,
    code_version:
      saveData.settings.codeVersion ??
      (saveData.settings.runWith === "code" ? 2 : undefined),
    workflow_definition: {
      version: saveData.workflowDefinitionVersion,
      parameters: saveData.parameters,
      blocks: saveData.blocks,
      finally_block_label: saveData.settings.finallyBlockLabel ?? null,
      workflow_system_prompt: saveData.settings.workflowSystemPrompt ?? null,
      error_code_mapping: saveData.settings.errorCodeMapping ?? null,
      retry_policy: saveData.settings.retryPolicy ?? null,
    },
    is_saved_task: saveData.workflow.is_saved_task,
    status: opts?.status ?? saveData.workflow.status,
    run_sequentially: saveData.settings.runSequentially,
    sequential_key: saveData.settings.sequentialKey,
  };
}

export function buildWorkflowYamlDocument(input: {
  workflow: WorkflowApiResponse;
  settings: WorkflowSettings;
  title: string;
  description: string | null;
  parameters: ParameterYAML[];
  blocks: BlockYAML[];
  definitionVersion: number;
}): WorkflowCreateYAMLRequest {
  const document = buildWorkflowSaveRequest({
    ...input,
    workflowDefinitionVersion: input.definitionVersion,
  });
  delete document.is_saved_task;
  delete document.status;
  return {
    ...document,
    extra_http_headers: input.settings.extraHttpHeaders
      ? document.extra_http_headers
      : null,
    cdp_connect_headers: input.settings.cdpConnectHeaders
      ? document.cdp_connect_headers
      : null,
    code_version: input.settings.codeVersion,
    ai_fallback: input.settings.aiFallback,
    workflow_definition: {
      ...document.workflow_definition,
      finally_block_label: input.settings.finallyBlockLabel,
      workflow_system_prompt: input.settings.workflowSystemPrompt,
      error_code_mapping: input.settings.errorCodeMapping,
    },
  };
}

function isGeoTargetProxyLocation(value: unknown): value is GeoTarget {
  if (!isPlainMapping(value) || typeof value.country !== "string") return false;
  const countryCode = value.country.toUpperCase();
  return (
    SUPPORTED_COUNTRY_CODES.some((country) => country === countryCode) &&
    Object.entries(value).every(([key, entry]) =>
      key === "isISP"
        ? typeof entry === "boolean"
        : ["country", "subdivision", "city"].includes(key) &&
          (typeof entry === "string" || (key !== "country" && entry === null)),
    )
  );
}

function isCustomProxyLocation(value: unknown): value is { url: string } {
  return (
    isPlainMapping(value) &&
    typeof value.url === "string" &&
    !("country" in value)
  );
}

function customProxyUrlHasHost(url: string): boolean {
  // Match urllib.parse's authority handling, including scheme-relative URLs.
  // WHATWG URL would also accept hostless "http:host".
  let normalized = url.replace(/[\t\n\r]/g, "");
  while (normalized.length && normalized.charCodeAt(0) <= 32) {
    normalized = normalized.slice(1);
  }
  const authority = normalized.match(
    /^(?:[a-z][a-z\d+.-]*:)?\/\/([^/?#]*)/i,
  )?.[1];
  const hostPort = authority?.slice(authority.lastIndexOf("@") + 1) ?? "";
  const host = hostPort.startsWith("[")
    ? hostPort.slice(1, Math.max(0, hostPort.indexOf("]")))
    : hostPort.split(":")[0];
  return Boolean(host);
}

function isPrivateProxyLocation(
  value: unknown,
): value is Record<string, unknown> {
  return isPlainMapping(value) && !isGeoTargetProxyLocation(value);
}

function proxyLocationsEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return (
      left.length === right.length &&
      left.every((entry, index) => proxyLocationsEqual(entry, right[index]))
    );
  }
  if (isPlainMapping(left) && isPlainMapping(right)) {
    return (
      Object.keys(left).length === Object.keys(right).length &&
      Object.entries(left).every(
        ([key, entry]) =>
          Object.prototype.hasOwnProperty.call(right, key) &&
          proxyLocationsEqual(entry, right[key]),
      )
    );
  }
  return false;
}

// Webhook and TOTP URLs can contain credentials in userinfo, paths, or queries.
const withheldCopilotSettings = [
  "cdp_connect_headers",
  "extra_http_headers",
  "totp_identifier",
  "totp_verification_url",
  "webhook_callback_url",
] as const;

export function buildWorkflowCopilotContext(
  input: Parameters<typeof buildWorkflowYamlDocument>[0],
): {
  document: WorkflowCreateYAMLRequest;
  snapshot: WorkflowCreateYAMLRequest;
} {
  const safeHeaders = (value: string | null): string | null => {
    if (!value) return null;
    try {
      const headers = parseHeaders(value, "headers");
      return headers ? JSON.stringify(headers) : null;
    } catch {
      // Invalid local JSON must not prevent a turn. Save still validates it.
      return null;
    }
  };
  const snapshot = buildWorkflowYamlDocument({
    ...input,
    settings: {
      ...input.settings,
      extraHttpHeaders: safeHeaders(input.settings.extraHttpHeaders),
      cdpConnectHeaders: safeHeaders(input.settings.cdpConnectHeaders),
    },
  });
  const document = { ...snapshot };
  for (const field of withheldCopilotSettings) delete document[field];
  const proxy = document.proxy_location;
  if (isPrivateProxyLocation(proxy)) {
    delete document.proxy_location;
  }
  return { snapshot, document };
}

export function restoreWorkflowCopilotSettings(
  workflow: WorkflowApiResponse,
  snapshot: WorkflowSettings,
  settings = apiWorkflowToSettings(workflow),
): { settings: WorkflowSettings; settingsChanged: boolean } {
  const snapshotProxy = snapshot.proxyLocation;
  const appliedProxy = workflow.proxy_location;
  let proxySettings: Pick<WorkflowSettings, "proxyLocation"> | undefined;
  // Null can be a schema default when Copilot never received the proxy.
  if (
    appliedProxy === undefined ||
    (appliedProxy === null && isPrivateProxyLocation(snapshotProxy))
  ) {
    proxySettings = { proxyLocation: snapshotProxy };
  }
  const proxyChanged =
    proxySettings !== undefined &&
    !proxyLocationsEqual(appliedProxy ?? null, proxySettings.proxyLocation);
  const restoredSettings = { ...settings };
  let withheldSettingsChanged = false;
  for (const field of withheldCopilotSettings) {
    const setting = settingFields[field];
    const value = snapshot[setting];
    restoredSettings[setting] = value;
    if (field === "cdp_connect_headers" || field === "extra_http_headers") {
      try {
        const restored = parseHeaders(value, field) ?? {};
        const applied = workflow[field] ?? {};
        if (
          Object.keys(restored).length !== Object.keys(applied).length ||
          Object.entries(restored).some(
            ([key, value]) => applied[key] !== value,
          )
        )
          withheldSettingsChanged = true;
      } catch {
        // Malformed local input still needs a normal Save to validate it.
        withheldSettingsChanged = true;
      }
    } else if ((workflow[field] ?? null) !== (value ?? null)) {
      withheldSettingsChanged = true;
    }
  }
  return {
    settings: {
      ...restoredSettings,
      ...proxySettings,
    },
    settingsChanged: withheldSettingsChanged || proxyChanged,
  };
}

const settingFields = {
  webhook_callback_url: "webhookCallbackUrl",
  proxy_location: "proxyLocation",
  persist_browser_session: "persistBrowserSession",
  reuse_browser_session: "reuseBrowserSession",
  pin_saved_session_ip: "pinSavedSessionIp",
  browser_profile_id: "browserProfileId",
  browser_profile_key: "browserProfileKey",
  model: "model",
  max_screenshot_scrolls: "maxScreenshotScrolls",
  max_elapsed_time_minutes: "maxElapsedTimeMinutes",
  extra_http_headers: "extraHttpHeaders",
  cdp_connect_headers: "cdpConnectHeaders",
  run_with: "runWith",
  browser_type: "browserType",
  code_version: "codeVersion",
  cache_key: "scriptCacheKey",
  ai_fallback: "aiFallback",
  mask_secrets: "maskSecrets",
  run_sequentially: "runSequentially",
  sequential_key: "sequentialKey",
  totp_verification_url: "totpVerificationUrl",
  totp_identifier: "totpIdentifier",
  adaptive_caching: "adaptiveCaching",
  generate_script_on_terminal: "generateScriptOnTerminal",
  finally_block_label: "finallyBlockLabel",
  workflow_system_prompt: "workflowSystemPrompt",
  error_code_mapping: "errorCodeMapping",
  retry_policy: "retryPolicy",
} as const satisfies Record<keyof SettingsPatch, keyof WorkflowSettings>;
const booleanFields = new Set([
  "persist_browser_session",
  "reuse_browser_session",
  "pin_saved_session_ip",
  "ai_fallback",
  "mask_secrets",
  "run_sequentially",
  "adaptive_caching",
  "generate_script_on_terminal",
]);
const integerRanges: Partial<Record<keyof SettingsPatch, [number, number]>> = {
  max_screenshot_scrolls: [0, 1000],
  max_elapsed_time_minutes: [1, 480],
  code_version: [1, 2],
};

export function validateSettingsPatch(patch: SettingsPatch): void {
  for (const [field, value] of Object.entries(patch)) {
    const fail = (message: string): never => {
      throw new YamlCommitError(field, message);
    };
    if (!Object.prototype.hasOwnProperty.call(settingFields, field))
      fail("unknown setting");
    if (value === null) continue;
    const range = integerRanges[field as keyof SettingsPatch];
    if (range) {
      if (
        typeof value !== "number" ||
        !Number.isInteger(value) ||
        value < range[0] ||
        value > range[1]
      )
        fail(`must be an integer from ${range[0]} to ${range[1]}`);
    } else if (booleanFields.has(field)) {
      if (typeof value !== "boolean") fail("must be a boolean or null");
    } else if (
      [
        "extra_http_headers",
        "cdp_connect_headers",
        "error_code_mapping",
      ].includes(field)
    ) {
      if (
        !isPlainMapping(value) ||
        Object.values(value).some((entry) => typeof entry !== "string")
      )
        fail("must be a mapping of strings to strings or null");
    } else if (field === "run_with") {
      if (
        typeof value !== "string" ||
        !["agent", "code", "ai", "code_v2"].includes(value)
      )
        fail("must be agent, code, ai, or code_v2");
    } else if (field === "proxy_location") {
      if (isGeoTargetProxyLocation(value)) {
        for (const [key, limit] of [
          ["subdivision", 10],
          ["city", 100],
        ] as const) {
          const entry = value[key];
          // Pydantic counts Unicode code points, not UTF-16 code units.
          if (typeof entry === "string" && Array.from(entry).length > limit) {
            throw new YamlCommitError(
              `${field}.${key}`,
              `must be at most ${limit} characters`,
            );
          }
        }
      } else if (isCustomProxyLocation(value)) {
        if (!customProxyUrlHasHost(value.url)) {
          throw new YamlCommitError(
            `${field}.url`,
            "custom proxy URL must include a host",
          );
        }
      } else if (typeof value !== "string")
        fail("unsupported proxy_location value in the editor");
    } else if (field === "model") {
      if (!isPlainMapping(value) || typeof value.model_name !== "string")
        fail("must be a mapping with a string model_name");
    } else if (field === "retry_policy") {
      validateRetryPolicyInput(value);
    } else if (typeof value !== "string") {
      fail("must be a string or null");
    }
  }
}

export function applySettingsPatch(
  current: WorkflowSettings,
  patch: SettingsPatch,
): WorkflowSettings {
  validateSettingsPatch(patch);
  const result = { ...current };
  for (const field of Object.keys(patch) as Array<keyof SettingsPatch>) {
    let value = patch[field];
    if (
      value === null &&
      ["code_version", "cdp_connect_headers", "mask_secrets"].includes(field)
    )
      continue;
    if (booleanFields.has(field)) value ??= field === "ai_fallback";
    if (field === "run_with")
      value = value === "code" || value === "code_v2" ? "code" : "agent";
    if (field === "cache_key") value = value || "default";
    if (field === "browser_profile_key")
      value = typeof value === "string" ? value.trim() || null : null;
    if (field === "extra_http_headers" || field === "cdp_connect_headers")
      value = value === null ? null : JSON.stringify(value);
    if (field === "retry_policy") value = validateRetryPolicyInput(value);
    Object.assign(result, { [settingFields[field]]: value });
  }
  return result;
}

export function resolveFinallyBlockLabel(
  current: string | null,
  patch: SettingsPatch,
  repairedBlocks: ReadonlyArray<{
    label: string;
    next_block_label?: string | null;
  }>,
): string | null {
  const terminalLabels = repairedBlocks
    .filter((block) => block.next_block_label == null)
    .map((block) => block.label);
  if (Object.prototype.hasOwnProperty.call(patch, "finally_block_label")) {
    if (patch.finally_block_label === null) return null;
    if (
      typeof patch.finally_block_label !== "string" ||
      !terminalLabels.includes(patch.finally_block_label)
    ) {
      throw new YamlCommitError(
        "finally_block_label",
        "must name a top-level terminal block",
      );
    }
    return patch.finally_block_label;
  }
  return preservedFinallyBlockLabel(current, terminalLabels);
}
