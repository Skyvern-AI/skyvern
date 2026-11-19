import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";
import { getModelDisplayLabel } from "@/components/modelLabels";

import {
  buildGroupSummaries,
  changedSettingCount,
} from "./agentSettingsSummary";
import type { WorkflowStartNodeData } from "./types";

const defaults: WorkflowStartNodeData = {
  withWorkflowSettings: true,
  retryPolicy: null,
  webhookCallbackUrl: "",
  proxyLocation: ProxyLocation.Residential,
  totpVerificationUrl: null,
  totpIdentifier: null,
  adaptiveCaching: false,
  generateScriptOnTerminal: false,
  persistBrowserSession: false,
  reuseBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  editable: true,
  runWith: "agent",
  codeVersion: null,
  scriptCacheKey: null,
  aiFallback: true,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  label: "__start_block__",
};

function boldCount(data: WorkflowStartNodeData) {
  return Object.values(buildGroupSummaries(data, { modelLabel: "Model X" }))
    .flat()
    .filter((segment) => segment.changed).length;
}

describe("agent settings summaries", () => {
  test("new-workflow defaults count as nothing changed", () => {
    expect(changedSettingCount(defaults)).toBe(0);
    const summaries = buildGroupSummaries(defaults, { modelLabel: "Model X" });
    expect(summaries.runs.map((s) => s.text).join(" · ")).toBe(
      "No retry · 4 h limit",
    );
  });

  test("a null proxy is a default", () => {
    const legacy = {
      ...defaults,
      proxyLocation: null,
    } as unknown as WorkflowStartNodeData;
    expect(changedSettingCount(legacy)).toBe(0);
  });

  test("stored code-mode settings are not counted as changes", () => {
    const code = {
      ...defaults,
      runWith: "code",
      aiFallback: false,
      scriptCacheKey: "k",
    };
    expect(changedSettingCount(code)).toBe(0);
  });

  test("the six settings from the brief each add one change and one bold segment", () => {
    const changes: Array<Partial<WorkflowStartNodeData>> = [
      { proxyLocation: ProxyLocation.ResidentialGB },
      { maxElapsedTimeMinutes: 60 },
      { maskSecrets: true },
      { webhookCallbackUrl: "https://hooks.example.com/a/b" },
      {
        retryPolicy: {
          max_retries: 2,
          delay_seconds: 0,
          webhook_on_retry: "final_only",
          retry_on: [{ status: "failed" }],
        },
      },
      { finallyBlockLabel: "cleanup" },
    ];
    let data = defaults;
    changes.forEach((change, index) => {
      data = { ...data, ...change };
      expect(changedSettingCount(data)).toBe(index + 1);
      expect(boldCount(data)).toBe(index + 1);
    });
    expect(buildGroupSummaries(data, {}).webhook[0]).toEqual({
      text: "hooks.example.com",
      changed: true,
      title: "https://hooks.example.com/a/b",
    });
  });

  test("browser type shows the select's label and is not counted without options", () => {
    const options = [{ value: "chromium-headful", label: "Chromium" }];
    const data = { ...defaults, browserType: "chromium-headful" };
    expect(
      buildGroupSummaries(data, { browserTypeOptions: options }).browser[0],
    ).toEqual({
      text: "Chromium",
      changed: true,
    });
    expect(changedSettingCount(data, { browserTypeOptions: options })).toBe(1);
    expect(changedSettingCount(data, { browserTypeOptions: [] })).toBe(0);
    expect(changedSettingCount(data)).toBe(0);
  });

  test("model label matches the selector, including deprecated and unknown models", () => {
    const models = { "gpt-x": "GPT X", "claude-opus-5": "Opus 5" };
    expect(getModelDisplayLabel(models, "gpt-x")).toBe("GPT X");
    expect(getModelDisplayLabel(models, "claude-opus-5")).toBe(
      "Opus 5 (deprecated)",
    );
    expect(getModelDisplayLabel(models, "gone")).toBe("Skyvern Optimized");
    expect(getModelDisplayLabel(undefined, null)).toBe("Skyvern Optimized");
    expect(getModelDisplayLabel(undefined, "gpt-x")).toBe("gpt-x");
  });
});
