import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";

import type { WorkflowBlock, WorkflowSettings } from "../types/workflowTypes";

import { getElements } from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
  persistBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  enableSelfHealing: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
};

describe("getElements is robust to blocks with undefined parameters", () => {
  test("a task block whose parameters is missing does not throw", () => {
    // Malformed / legacy persisted workflows can omit parameters entirely, which
    // violates the WorkflowBlock type; convertToNode previously called
    // block.parameters.map() unconditionally and crashed on load.
    const block = {
      label: "task_1",
      block_type: "task",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      // parameters intentionally omitted (undefined at runtime)
    } as unknown as WorkflowBlock;

    expect(() => getElements([block], DEFAULT_SETTINGS, false)).not.toThrow();
    const { nodes } = getElements([block], DEFAULT_SETTINGS, false);
    const taskNode = nodes.find((node) => node.type === "task");
    expect(taskNode).toBeDefined();
  });
});
