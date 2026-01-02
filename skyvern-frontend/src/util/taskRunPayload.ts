import type { CreateTaskRequest, ProxyLocation, RunEngine } from "@/api/types";

type TaskRunPayload = {
  prompt: string;
  url?: string | null;
  proxy_location?: ProxyLocation | null;
  data_extraction_schema?: CreateTaskRequest["extracted_information_schema"];
  error_code_mapping?: Record<string, string> | null;
  extra_http_headers?: Record<string, string> | null;
  webhook_url?: string | null;
  totp_identifier?: string | null;
  browser_address?: string | null;
  include_action_history_in_verification?: boolean | null;
  max_screenshot_scrolls?: number | null;
  title?: string | null;
  engine?: RunEngine | null;
};

// Helper to trim and check for empty strings
const trim = (s: string | null | undefined): string | undefined => {
  const t = s?.trim();
  return t && t.length > 0 ? t : undefined;
};

// Helper to format navigation_payload as a string
function formatNavigationPayload(
  payload: Record<string, unknown> | string | null | undefined,
): string | undefined {
  if (payload == null) return undefined;
  if (typeof payload === "string") {
    const trimmed = payload.trim();
    return trimmed.length > 0 ? trimmed : undefined;
  }
  if (typeof payload === "object" && !Array.isArray(payload)) {
    try {
      const jsonStr = JSON.stringify(payload);
      return jsonStr.length > 0 ? jsonStr : undefined;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

// Build prompt from navigation_goal + data_extraction_goal + navigation_payload
function buildPrompt(request: CreateTaskRequest): string {
  const nav = trim(request.navigation_goal);
  const extract = trim(request.data_extraction_goal);
  const payload = formatNavigationPayload(request.navigation_payload);

  const parts = [nav, extract, payload].filter(Boolean);
  if (parts.length > 0) return parts.join("\n\n");

  // Fallback chain: try title, then goals again, then url, then default
  return (
    trim(request.title) ??
    nav ??
    extract ??
    trim(request.url) ??
    "Task run triggered from Skyvern UI"
  );
}

function isValidRecord(val: unknown): val is Record<string, string> {
  return val != null && typeof val === "object" && !Array.isArray(val);
}

/**
 * Transforms a CreateTaskRequest (old schema) to TaskRunPayload (Runs API v2 schema).
 *
 * Key transformations:
 * - navigation_goal + data_extraction_goal + navigation_payload → prompt (combined)
 * - extracted_information_schema → data_extraction_schema
 * - webhook_callback_url → webhook_url
 *
 * Note: max_steps is optional and can be added manually to the cURL if needed.
 */
function buildTaskRunPayload(
  request: CreateTaskRequest,
  engine?: RunEngine | null,
): TaskRunPayload {
  const payload: TaskRunPayload = {
    prompt: buildPrompt(request),
    url: trim(request.url) ?? null,
    proxy_location: request.proxy_location ?? null,
    data_extraction_schema: request.extracted_information_schema,
    webhook_url: trim(request.webhook_callback_url),
    totp_identifier: trim(request.totp_identifier),
    browser_address: trim(request.browser_address),
    include_action_history_in_verification:
      request.include_action_history_in_verification,
    max_screenshot_scrolls: request.max_screenshot_scrolls,
    title: trim(request.title),
    extra_http_headers: isValidRecord(request.extra_http_headers)
      ? request.extra_http_headers
      : undefined,
    error_code_mapping: isValidRecord(request.error_code_mapping)
      ? request.error_code_mapping
      : undefined,
  };
  if (engine) {
    payload.engine = engine;
  }
  return payload;
}

export type { TaskRunPayload };
export { buildTaskRunPayload };
