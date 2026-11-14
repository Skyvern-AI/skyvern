import { ProxyLocation } from "@/api/types";
import {
  formatGeoTargetCompact,
  proxyLocationToGeoTarget,
} from "@/util/geoData";

import {
  hasBrowserTypeOptions,
  type BrowserTypeOption,
} from "@/routes/workflows/utils";

import type { WorkflowStartNodeData } from "./types";

type SummaryLabels = {
  modelLabel?: string;
  // The select only renders when the backend lists browser types; a value stored without it is not a visible change.
  browserTypeOptions?: Array<BrowserTypeOption>;
};

type GroupId = "model" | "runs" | "safety" | "browser" | "webhook";

type SummarySegment = { text: string; changed: boolean; title?: string };

type ChangedSetting = {
  key: string;
  group: GroupId;
  text: string;
  title?: string;
};

const GROUP_IDS: ReadonlyArray<GroupId> = [
  "model",
  "runs",
  "safety",
  "browser",
  "webhook",
];

function proxyLabel(value: WorkflowStartNodeData["proxyLocation"]): string {
  return formatGeoTargetCompact(
    proxyLocationToGeoTarget(value ?? ProxyLocation.Residential),
  );
}

function webhookHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function headerCount(headers: WorkflowStartNodeData["extraHttpHeaders"]) {
  if (!headers) return 0;
  try {
    const parsed = typeof headers === "string" ? JSON.parse(headers) : headers;
    return parsed && typeof parsed === "object"
      ? Object.keys(parsed).length
      : 0;
  } catch {
    return 0;
  }
}

function changedSettings(
  data: WorkflowStartNodeData,
  labels: SummaryLabels,
): Array<ChangedSetting> {
  const out: Array<ChangedSetting> = [];
  const add = (
    on: unknown,
    key: string,
    group: GroupId,
    text: string | (() => string),
    title?: string,
  ) => {
    if (on)
      out.push({
        key,
        group,
        text: typeof text === "function" ? text() : text,
        title,
      });
  };
  const headers = headerCount(data.extraHttpHeaders);

  add(data.model?.model_name, "model", "model", labels.modelLabel ?? "");
  add(data.workflowSystemPrompt, "prompt", "model", "Custom prompt");

  add(
    data.retryPolicy,
    "retry",
    "runs",
    () => `Retry up to ${data.retryPolicy?.max_retries ?? 1}x`,
  );
  add(
    data.maxElapsedTimeMinutes,
    "maxTime",
    "runs",
    `${data.maxElapsedTimeMinutes} min limit`,
  );
  add(
    data.finallyBlockLabel,
    "finally",
    "runs",
    `Finally: ${data.finallyBlockLabel}`,
  );

  add(data.maskSecrets, "mask", "safety", "Secrets masked");
  add(data.runSequentially, "overlap", "safety", "No overlapping runs");

  add(
    data.proxyLocation != null &&
      proxyLabel(data.proxyLocation) !== proxyLabel(ProxyLocation.Residential),
    "proxy",
    "browser",
    () => proxyLabel(data.proxyLocation),
  );
  const browserTypeOptions = labels.browserTypeOptions;
  add(
    data.browserType && hasBrowserTypeOptions(browserTypeOptions),
    "browserType",
    "browser",
    () =>
      browserTypeOptions?.find((option) => option.value === data.browserType)
        ?.label ?? String(data.browserType),
  );
  add(data.reuseBrowserSession, "reuse", "browser", "Reused session");
  add(
    data.persistBrowserSession && !data.reuseBrowserSession,
    "profile",
    "browser",
    "Saved profile",
  );
  add(data.browserProfileId, "startProfile", "browser", "Starting profile");
  add(
    headers,
    "headers",
    "browser",
    `${headers} header${headers === 1 ? "" : "s"}`,
  );
  add(
    data.maxScreenshotScrolls != null,
    "scrolls",
    "browser",
    `${data.maxScreenshotScrolls} scrolls`,
  );

  add(
    data.webhookCallbackUrl,
    "webhook",
    "webhook",
    () => webhookHost(data.webhookCallbackUrl),
    data.webhookCallbackUrl ?? undefined,
  );
  return out;
}

function changedSettingCount(
  data: WorkflowStartNodeData,
  labels: SummaryLabels = {},
): number {
  return changedSettings(data, labels).length;
}

function buildGroupSummaries(
  data: WorkflowStartNodeData,
  labels: SummaryLabels,
): Record<GroupId, Array<SummarySegment>> {
  const changed = changedSettings(data, labels);
  const defaults: Record<GroupId, Array<[string, string]>> = {
    model: [["model", "Skyvern Optimized"]],
    runs: [
      ["retry", "No retry"],
      ["maxTime", "4 h limit"],
    ],
    safety: [
      ["mask", "Secrets shown"],
      ["overlap", "Overlap allowed"],
    ],
    browser: [
      ["proxy", proxyLabel(ProxyLocation.Residential)],
      ["reuse", "Fresh session"],
    ],
    webhook: [["webhook", "No webhook"]],
  };
  const out = {} as Record<GroupId, Array<SummarySegment>>;
  for (const group of GROUP_IDS) {
    const inGroup = changed.filter((item) => item.group === group);
    const changedKeys = new Set(inGroup.map((item) => item.key));
    out[group] = [
      ...inGroup.map((item) => ({
        text: item.text,
        changed: true,
        title: item.title,
      })),
      ...defaults[group]
        .filter(([key]) => !changedKeys.has(key))
        .map(([, text]) => ({ text, changed: false })),
    ];
  }
  return out;
}

export {
  buildGroupSummaries,
  changedSettingCount,
  changedSettings,
  type GroupId,
  type SummaryLabels,
  type SummarySegment,
};
