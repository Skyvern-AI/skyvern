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
  showCode: false,
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
    expect(summaries.runwith.map((s) => s.text).join(" · ")).toBe(
      "Skyvern Agent",
    );
  });

  test("null proxy and null run_with are defaults, and the agent shows muted", () => {
    const legacy = {
      ...defaults,
      proxyLocation: null,
      runWith: null,
    } as unknown as WorkflowStartNodeData;
    expect(changedSettingCount(legacy)).toBe(0);
    expect(buildGroupSummaries(legacy, {}).runwith).toEqual([
      { text: "Skyvern Agent", changed: false },
    ]);
  });

  test("the seven settings from the brief each add one change and one bold segment", () => {
    const changes: Array<Partial<WorkflowStartNodeData>> = [
      { proxyLocation: ProxyLocation.ResidentialGB },
      { maxElapsedTimeMinutes: 60 },
      { maskSecrets: true },
      { webhookCallbackUrl: "https://hooks.example.com/a/b" },
      { runWith: "code" },
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

  test("Code is a change, and AI fallback off and a code key each add one", () => {
    const code = { ...defaults, runWith: "code" };
    expect(changedSettingCount(code)).toBe(1);
    expect(buildGroupSummaries(code, {}).runwith).toEqual([
      { text: "Code", changed: true, title: undefined },
      { text: "AI fallback on", changed: false },
    ]);
    expect(changedSettingCount({ ...code, aiFallback: false })).toBe(2);
    expect(changedSettingCount({ ...code, scriptCacheKey: "k" })).toBe(2);
    expect(
      changedSettingCount({ ...code, aiFallback: false, scriptCacheKey: "k" }),
    ).toBe(3);
  });

  test("hidden AI fallback and code key are not counted when running the agent", () => {
    for (const runWith of ["agent", null]) {
      const data = {
        ...defaults,
        runWith,
        aiFallback: false,
        scriptCacheKey: "k",
      } as unknown as WorkflowStartNodeData;
      expect(changedSettingCount(data)).toBe(0);
    }
  });

  test("the backend's default code key is not a change", () => {
    const code = { ...defaults, runWith: "code" };
    expect(changedSettingCount({ ...code, scriptCacheKey: "default" })).toBe(1);
    expect(changedSettingCount({ ...code, scriptCacheKey: "mine" })).toBe(2);
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
