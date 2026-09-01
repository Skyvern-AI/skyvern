import { describe, expect, test } from "vitest";

import { ProxyLocation, RunEngine } from "@/api/types";

import type {
  DataExportBlock,
  WorkflowBlock,
  WorkflowSettings,
} from "../types/workflowTypes";

import { getElements, getWorkflowBlocks } from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
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
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  enableSelfHealing: false,
  maskSecrets: false,
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

describe("engine round-trips through node data", () => {
  test("a task block pinned to skyvern-3.0 serializes back with that engine", () => {
    const block = {
      label: "task_1",
      block_type: "task",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      parameters: [],
      engine: RunEngine.SkyvernV3,
    } as unknown as WorkflowBlock;

    const { nodes, edges } = getElements([block], DEFAULT_SETTINGS, true);
    const taskNode = nodes.find((node) => node.type === "task");
    expect(taskNode?.data).toMatchObject({ engine: RunEngine.SkyvernV3 });

    const [savedBlock] = getWorkflowBlocks(nodes, edges);
    expect(savedBlock).toMatchObject({ engine: "skyvern-3.0" });
  });
});

describe("data export blocks", () => {
  test("an API-authored export block loads and saves without changing its contract", () => {
    const block = {
      label: "export_records",
      block_type: "data_export",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      parameters: [],
      data: "{{ extraction_output.extracted_information }}",
      data_schema: {
        type: "array",
        items: {
          type: "object",
          properties: { id: { type: "integer" } },
        },
      },
      file_name: "records",
    } as unknown as DataExportBlock;

    const { nodes, edges } = getElements([block], DEFAULT_SETTINGS, true);
    const exportNode = nodes.find(
      (node) => node.data.label === "export_records",
    );

    expect(exportNode?.type).toBe("dataExport");
    expect(exportNode?.data).toMatchObject({
      data: "{{ extraction_output.extracted_information }}",
      fileName: "records",
    });
    expect(getWorkflowBlocks(nodes, edges)).toEqual([
      expect.objectContaining({
        block_type: "data_export",
        data: "{{ extraction_output.extracted_information }}",
        data_schema: block.data_schema,
        file_name: "records",
      }),
    ]);
  });
});
