import { describe, expect, test } from "vitest";

import { ProxyLocation, RunEngine } from "@/api/types";

import type {
  DataExportBlock,
  ExtractionBlock,
  WorkflowApiResponse,
  WebSearchBlock,
  WorkflowBlock,
  WorkflowSettings,
} from "../types/workflowTypes";

import {
  type WebSearchNode,
  webSearchNodeDefaultData,
} from "./nodes/WebSearchNode/types";
import {
  convert,
  getElements,
  getWorkflowBlocks,
  getWorkflowErrors,
} from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
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
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  retryPolicy: null,
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

describe("extraction block export fields (SKY-15396)", () => {
  test("export_data_schema survives a load/save round trip while export is disabled", () => {
    // The schema must persist even with export_enabled: false, or toggling
    // export off then back on silently loses whatever the user authored.
    const exportDataSchema = {
      type: "array",
      items: {
        type: "object",
        properties: { name: { type: "string" } },
      },
    };
    const block = {
      label: "extract",
      block_type: "extraction",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      parameters: [],
      data_extraction_goal: "extract names",
      data_schema: null,
      export_enabled: false,
      export_data_schema: exportDataSchema,
      export_file_name: "names",
      export_records: null,
    } as unknown as ExtractionBlock;

    const { nodes, edges } = getElements([block], DEFAULT_SETTINGS, true);
    const [savedBlock] = getWorkflowBlocks(nodes, edges);

    expect(savedBlock).toMatchObject({
      export_enabled: false,
      export_data_schema: exportDataSchema,
      export_file_name: "names",
    });
  });
});

test("search Error Messages serialize and use standard mapping validation", () => {
  const node: WebSearchNode = {
    id: "search",
    type: "web_search",
    position: { x: 0, y: 0 },
    data: { ...webSearchNodeDefaultData, label: "search", query: "documents" },
  };

  for (const mapping of [null, { NO_RESULTS: "No results were found." }]) {
    node.data.errorCodeMapping = JSON.stringify(mapping);
    expect(getWorkflowBlocks([node], [])).toEqual([
      expect.objectContaining({
        block_type: "web_search",
        error_code_mapping: mapping,
        no_results_error_code: null,
        no_match_error_code: null,
      }),
    ]);
    expect(getWorkflowErrors([node])).toEqual([]);
  }

  for (const mapping of ["{", "[]", '{" CODE ":"No match"}']) {
    node.data.errorCodeMapping = mapping;
    expect(getWorkflowErrors([node])).toEqual([
      expect.stringContaining("search: Error messages"),
    ]);
  }
});

test("search without a prompt folds legacy codes and validates its Data Schema", () => {
  const node: WebSearchNode = {
    id: "search",
    type: "web_search",
    position: { x: 0, y: 0 },
    data: {
      ...webSearchNodeDefaultData,
      label: "search",
      query: "documents",
      prompt: " \t ",
      errorCodeMapping: '{"NO_MATCH":"No matching result."}',
      jsonSchema: '{"type":"object"}',
    },
  };

  for (const [legacyResults, legacyMatch, explicit, expected] of [
    [" ", "\t", null, null],
    [
      " NO_RESULTS ",
      " NO_MATCH ",
      null,
      {
        NO_RESULTS: "The search returned no results.",
        NO_MATCH: "No search result satisfies the Prompt.",
      },
    ],
    [
      " SAME ",
      "SAME",
      null,
      {
        SAME: "The search returned no results, or no search result satisfies the Prompt.",
      },
    ],
    [
      "SAME",
      "SAME",
      { SAME: "Explicit description" },
      { SAME: "Explicit description" },
    ],
    ["BAD\u0000CODE", "A".repeat(129), null, null],
    [
      "__proto__",
      null,
      null,
      { ["__proto__"]: "The search returned no results." },
    ],
  ] as const) {
    const block: WebSearchBlock = {
      output_parameter: {
        parameter_type: "output",
        key: "search_output",
        description: null,
        output_parameter_id: "output_search",
        workflow_id: "workflow_search",
        created_at: "2026-01-01T00:00:00Z",
        modified_at: "2026-01-01T00:00:00Z",
        deleted_at: null,
      },
      block_type: "web_search",
      label: "search",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      query: "documents",
      provider: "auto",
      num_results: 10,
      prompt: null,
      json_schema: null,
      parameters: [],
      error_code_mapping: explicit,
      no_results_error_code: legacyResults,
      no_match_error_code: legacyMatch,
    };
    const { nodes, edges } = getElements([block], DEFAULT_SETTINGS, true);
    const searchNode = nodes.find((entry) => entry.type === "web_search");
    expect(searchNode?.data.errorCodeMapping).toBe(
      JSON.stringify(expected, null, 2),
    );
    const saved = {
      error_code_mapping: expected,
      no_results_error_code: null,
      no_match_error_code: null,
    };
    expect(getWorkflowBlocks(nodes, edges)[0]).toMatchObject(saved);
    const workflow = {
      workflow_definition: { blocks: [block], parameters: [] },
    } as unknown as WorkflowApiResponse;
    expect(convert(workflow).workflow_definition.blocks[0]).toMatchObject(
      saved,
    );
  }

  expect(getWorkflowBlocks([node], [])).toEqual([
    expect.objectContaining({
      block_type: "web_search",
      error_code_mapping: { NO_MATCH: "No matching result." },
      no_match_error_code: null,
      no_results_error_code: null,
      json_schema: { type: "object" },
    }),
  ]);
  expect(getWorkflowErrors([node])).toEqual([]);
  node.data.jsonSchema = "{";
  expect(getWorkflowErrors([node])).toEqual([
    expect.stringContaining("search: Data schema -"),
  ]);
});
