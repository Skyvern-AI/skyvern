import { isRecord } from "@/util/utils";
import { UNFINISHED_OUTCOMES } from "../ember/cards/runGroup";
import type { AccountGroupOutcome } from "../ember/emberTypes";
import {
  isTaskVariantBlock,
  type WorkflowBlockType,
} from "../types/workflowTypes";
import { isCappedValue, isTruncationMarker } from "./responseValueCaps";

// A cell starting with a formula or control character gets a leading quote, so spreadsheets read it as text.
const CSV_FORMULA_PREFIXES = ["=", "+", "-", "@"];
const CSV_CONTROL_PREFIXES = ["\t", "\r", "\n"];
// Mirrors is_sensitive_workflow_parameter in skyvern/forge/sdk/workflow/models/parameter.py.
const SECRET_PARAMETER_TYPES: ReadonlySet<string> = new Set([
  "aws_secret",
  "bitwarden_login_credential",
  "bitwarden_sensitive_information",
  "bitwarden_credit_card_data",
  "onepassword",
  "azure_vault_credential",
  "credential",
  "azure_secret",
]);
// REDACTED_ARTIFACT and REDACTED_SECRET_PLACEHOLDER on the backend; SCRUBBED_VALUE marks a value retention removed.
const REDACTION_MARKERS: ReadonlySet<string> = new Set([
  "[redacted]",
  "[REDACTED_SECRET]",
]);
const SCRUBBED_VALUE = "[removed]";
const ENVELOPE_URL_KEYS: ReadonlySet<string> = new Set([
  "downloaded_file_urls",
  "task_screenshots",
  "workflow_screenshots",
  "artifact_url",
]);
const ENVELOPE_URL_LIST_FIELDS: ReadonlyArray<[string, string]> = [
  ["downloaded_files", "url"],
  ["documents", "artifact_url"],
];
const LINK_ENVELOPE_BLOCK_TYPES: ReadonlySet<WorkflowBlockType> = new Set([
  "print_page",
  "pdf_fill",
  "split_pdf",
  "data_export",
]);
const LOOP_BLOCK_TYPES: ReadonlySet<WorkflowBlockType> = new Set([
  "for_loop",
  "while_loop",
]);

const UNAVAILABLE = "[unavailable]";
const TRUNCATED = "[truncated]";

type OutputState =
  | "recorded"
  | "pending"
  | "unavailable"
  | "redacted"
  | "truncated"
  | "omitted";

type RunBlock = {
  block_type: WorkflowBlockType;
  output_parameter: { key: string };
  loop_blocks?: RunBlock[];
};

type GroupRunRead = {
  status: string;
  created_at: string;
  finished_at: string | null;
  parameters: Record<string, unknown>;
  outputs: Record<string, unknown> | null;
  workflow: {
    modified_at: string;
    workflow_definition: {
      parameters: Array<{
        key: string;
        parameter_type: string;
        workflow_parameter_type?: string;
      }>;
      blocks: RunBlock[];
    };
  };
};

type GroupRowSnapshot = {
  key: string;
  outcome: AccountGroupOutcome;
  run_status: string | null;
};

type FinalOutput = {
  state: OutputState;
  value?: unknown;
  reason?: "definition_changed";
};

type GroupCsvRow = {
  position: number;
  outcome: string;
  workflowRunId: string | null;
  // null: no inputs were read for the row.
  inputs: Record<string, unknown> | null;
  output: FinalOutput;
};

function csvSafeCell(value: string): string {
  if (
    [...CSV_FORMULA_PREFIXES, ...CSV_CONTROL_PREFIXES].some((prefix) =>
      value.startsWith(prefix),
    ) ||
    CSV_FORMULA_PREFIXES.some((prefix) => value.trimStart().startsWith(prefix))
  ) {
    return `'${value}`;
  }
  return value;
}

function cellText(value: unknown): string {
  if (value === undefined) {
    return UNAVAILABLE;
  }
  if (value === null) {
    return "null";
  }
  if (isTruncationMarker(value)) {
    return TRUNCATED;
  }
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return isCappedValue(value) ? `${TRUNCATED} ${text}` : text;
}

function isScrubbedKey(key: string): boolean {
  return key.startsWith(SCRUBBED_VALUE);
}

// A run's inputs keep the values they ran with, but the definition here is the current one, so after any in-place
// edit it cannot say which keys were credentials when the run ran and none are shown.
function safeInputs(run: GroupRunRead): Record<string, unknown> | null {
  if (Date.parse(run.workflow.modified_at) > Date.parse(run.created_at)) {
    return null;
  }
  const declared = new Set(
    run.workflow.workflow_definition.parameters
      .filter(
        (parameter) =>
          !SECRET_PARAMETER_TYPES.has(parameter.parameter_type) &&
          !(
            parameter.parameter_type === "workflow" &&
            parameter.workflow_parameter_type === "credential_id"
          ),
      )
      .map((parameter) => parameter.key),
  );
  return Object.fromEntries(
    Object.entries(run.parameters).filter(([key]) => declared.has(key)),
  );
}

// The final output is what the last top-level block of the version the run executed recorded under its output key.
function finalOutput(
  row: GroupRowSnapshot,
  run: GroupRunRead | null | undefined,
): FinalOutput {
  if (UNFINISHED_OUTCOMES.has(row.outcome)) {
    return { state: "pending" };
  }
  if (!run || isScrubbedKey(row.key)) {
    return { state: "unavailable" };
  }
  if (run.status !== row.run_status) {
    return { state: "pending" };
  }
  if (
    run.finished_at !== null &&
    Date.parse(run.workflow.modified_at) > Date.parse(run.finished_at)
  ) {
    return { state: "unavailable", reason: "definition_changed" };
  }
  const blocks = run.workflow.workflow_definition.blocks;
  const lastBlock = blocks[blocks.length - 1];
  const key = lastBlock?.output_parameter.key;
  if (key === undefined || !run.outputs || !(key in run.outputs)) {
    return { state: "unavailable" };
  }
  const recorded = run.outputs[key];
  if (recorded === SCRUBBED_VALUE) {
    return { state: "unavailable" };
  }
  if (typeof recorded === "string" && REDACTION_MARKERS.has(recorded)) {
    return { state: "redacted", value: recorded };
  }
  const value = withoutArtifactUrls(
    recorded,
    lastBlock!.block_type,
    blockTypesByKey([lastBlock!]),
  );
  if (isCappedValue(recorded)) {
    return { state: "truncated", value };
  }
  return { state: "recorded", value };
}

function blockTypesByKey(
  blocks: RunBlock[],
  types: Map<string, WorkflowBlockType> = new Map(),
): Map<string, WorkflowBlockType> {
  for (const block of blocks) {
    types.set(block.output_parameter.key, block.block_type);
    blockTypesByKey(block.loop_blocks ?? [], types);
  }
  return types;
}

function hasLink(value: unknown): boolean {
  return Array.isArray(value) ? value.some(Boolean) : Boolean(value);
}

function withoutArtifactUrls(
  value: unknown,
  blockType: WorkflowBlockType | undefined,
  types: Map<string, WorkflowBlockType>,
): unknown {
  if (blockType && LOOP_BLOCK_TYPES.has(blockType) && Array.isArray(value)) {
    return value.map((iteration) =>
      Array.isArray(iteration)
        ? iteration.map((entry) => {
            const parameter = isRecord(entry) ? entry.output_parameter : null;
            const childKey = isRecord(parameter) ? parameter.key : null;
            return isRecord(entry) &&
              typeof childKey === "string" &&
              "output_value" in entry
              ? {
                  ...entry,
                  output_value: withoutArtifactUrls(
                    entry.output_value,
                    types.get(childKey),
                    types,
                  ),
                }
              : entry;
          })
        : iteration,
    );
  }
  if (
    !blockType ||
    !(
      isTaskVariantBlock({ block_type: blockType }) ||
      LINK_ENVELOPE_BLOCK_TYPES.has(blockType)
    ) ||
    !isRecord(value)
  ) {
    return value;
  }
  const kept: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(value)) {
    if (!(ENVELOPE_URL_KEYS.has(key) && hasLink(field))) {
      kept[key] = field;
    }
  }
  for (const [listKey, urlKey] of ENVELOPE_URL_LIST_FIELDS) {
    const listed = kept[listKey];
    if (Array.isArray(listed)) {
      kept[listKey] = listed.map((entry) =>
        isRecord(entry) && entry[urlKey]
          ? Object.fromEntries(
              Object.entries(entry).filter(([field]) => field !== urlKey),
            )
          : entry,
      );
    }
  }
  return kept;
}

function isFlatRecord(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    Object.values(value).every(
      (field) => field === null || typeof field !== "object",
    )
  );
}

function elementFields(listName: string, element: unknown) {
  const fields: Record<string, string> = { list_name: listName };
  if (isRecord(element)) {
    for (const [key, value] of Object.entries(element)) {
      fields[`item:${key}`] = cellText(value);
    }
  } else {
    fields.item = cellText(element);
  }
  return fields;
}

// A top-level list of flat records becomes one line per record; an object with named lists becomes one line per
// list element, carrying the list name and the object's other fields.
function outputLines(output: FinalOutput): Record<string, string>[] {
  if (output.state !== "recorded" || output.value === undefined) {
    return [
      {
        output:
          output.state === "redacted" || output.state === "truncated"
            ? cellText(output.value)
            : (output.reason ?? ""),
      },
    ];
  }
  const value = output.value;
  if (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((element) => isFlatRecord(element))
  ) {
    return value.map((element) => elementFields("", element));
  }
  if (isRecord(value) && Object.values(value).some(Array.isArray)) {
    const base: Record<string, string> = {};
    const lines: Record<string, string>[] = [];
    for (const [field, child] of Object.entries(value)) {
      if (Array.isArray(child) && child.length > 0) {
        for (const element of child) {
          lines.push(elementFields(field, element));
        }
      } else {
        base[`output:${field}`] = cellText(child);
      }
    }
    return lines.length > 0
      ? lines.map((line) => ({ ...base, ...line }))
      : [base];
  }
  return [{ output: cellText(value) }];
}

function columnOrder(column: string): number {
  if (column === "output") return 0;
  if (column.startsWith("output:")) return 1;
  if (column === "list_name") return 2;
  if (column === "item") return 3;
  return 4;
}

function quote(cell: string): string {
  const safe = csvSafeCell(cell);
  return /[",\r\n]/.test(safe) ? `"${safe.split('"').join('""')}"` : safe;
}

// One string per CSV line, so a large export never builds a single string past the engine's length limit.
function buildRunGroupCsv(rows: GroupCsvRow[], origin: string): string[] {
  const inputKeys = [
    ...new Set(rows.flatMap((row) => Object.keys(row.inputs ?? {}))),
  ];
  const expanded = rows.map((row) => outputLines(row.output));
  const outputColumns = [
    ...new Set(expanded.flat().flatMap((line) => Object.keys(line))),
  ];
  outputColumns.sort((a, b) => columnOrder(a) - columnOrder(b));
  const header = [
    "row",
    "outcome",
    "run",
    ...inputKeys.map((key) => `input:${key}`),
    "output_state",
    ...outputColumns,
  ];
  const lines = [header.map(quote).join(",")];
  rows.forEach((row, index) => {
    const base = [
      String(row.position + 1),
      row.outcome,
      row.workflowRunId ? `${origin}/runs/${row.workflowRunId}` : "",
      ...inputKeys.map((key) =>
        row.inputs ? cellText(row.inputs[key]) : UNAVAILABLE,
      ),
      row.output.state,
    ];
    for (const line of expanded[index]!) {
      const cells = [
        ...base,
        ...outputColumns.map((column) => line[column] ?? ""),
      ];
      lines.push(`\r\n${cells.map(quote).join(",")}`);
    }
  });
  return lines;
}

export {
  buildRunGroupCsv,
  cellText,
  csvSafeCell,
  finalOutput,
  isScrubbedKey,
  safeInputs,
};
export type { FinalOutput, GroupCsvRow, GroupRunRead, OutputState };
