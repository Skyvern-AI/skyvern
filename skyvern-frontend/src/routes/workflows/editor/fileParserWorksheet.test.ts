import { describe, expect, test } from "vitest";

import type {
  FileURLParserBlock,
  OutputParameter,
  WorkflowApiResponse,
} from "../types/workflowTypes";
import type { FileUrlParserBlockYAML } from "../types/workflowYamlTypes";

import { isFileParserNode } from "./nodes/FileParserNode/types";
import {
  convert,
  convertToNode,
  getWorkflowBlocks,
} from "./workflowEditorUtils";

const OUTPUT_PARAMETER: OutputParameter = {
  parameter_type: "output",
  key: "parse_workbook_output",
  description: null,
  output_parameter_id: "op_1",
  workflow_id: "w_1",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  deleted_at: null,
};

const FILE_PARSER_BLOCK: FileURLParserBlock = {
  label: "parse_workbook",
  next_block_label: null,
  block_type: "file_url_parser",
  output_parameter: OUTPUT_PARAMETER,
  continue_on_failure: false,
  next_loop_on_failure: false,
  model: null,
  file_url: "https://example.com/workbook.xlsx",
  file_type: "excel",
  json_schema: null,
  worksheet: "Query Log",
};

describe("File Parser worksheet round-trip", () => {
  test("an authored worksheet survives block -> node -> block", () => {
    const node = convertToNode({ id: "fp1" }, FILE_PARSER_BLOCK, true);
    expect(isFileParserNode(node)).toBe(true);
    if (!isFileParserNode(node)) {
      return;
    }
    expect(node.data.worksheet).toBe("Query Log");

    const [block] = getWorkflowBlocks([node], []);

    expect((block as FileUrlParserBlockYAML).worksheet).toBe("Query Log");
  });

  test("an authored worksheet survives the workflow -> YAML conversion", () => {
    const workflow = {
      title: "t",
      workflow_definition: {
        version: 2,
        parameters: [],
        blocks: [FILE_PARSER_BLOCK],
      },
    } as unknown as WorkflowApiResponse;

    const [block] = convert(workflow).workflow_definition.blocks;

    expect((block as FileUrlParserBlockYAML).worksheet).toBe("Query Log");
  });
});
