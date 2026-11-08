import { Status } from "@/api/types";
import {
  getBlockOutputDisplayValue,
  shouldShowExtractedInformation,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import { isTaskVariantBlock } from "../../types/workflowTypes";
import { describeCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { formatFailureReason } from "../../workflowRun/failureReasonFormat";
import { NO_START_PAGE_TEXT, plainFailureText } from "./docFailureText";
import { blockFailureHeadline } from "./docRunState";

export const TABLE_ROW_CAP = 5;

export type DocProduced =
  | {
      kind: "table";
      columns: string[];
      rows: Array<Record<string, unknown>>;
      moreRows: number;
    }
  | { kind: "pairs"; pairs: Array<[string, unknown]> }
  | { kind: "json"; value: unknown };

const isRecord = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  if (isRecord(value)) return Object.keys(value).length === 0;
  return false;
}

// Loops and branches report through their own lines; a task's envelope (ids, status) is not its result.
const NO_RESULT_VALUE = new Set(["for_loop", "while_loop", "conditional"]);

function producedValue(block: WorkflowRunBlock): unknown {
  if (block.status !== Status.Completed) return null;
  if (NO_RESULT_VALUE.has(block.block_type)) return null;
  if (shouldShowExtractedInformation(block)) {
    return getBlockOutputDisplayValue(block);
  }
  if (isTaskVariantBlock(block)) return null;
  const output = block.output;
  if (!isRecord(output)) return output;
  // A bare success flag only Wait and Send email steps emit says nothing; files get their own links.
  const rest = Object.entries(output).filter(
    ([key]) => key !== "downloaded_file_urls",
  );
  if (
    (block.block_type === "wait" || block.block_type === "send_email") &&
    rest.length === 1 &&
    rest[0]![0] === "success"
  )
    return null;
  return Object.fromEntries(rest);
}

const isScalar = (v: unknown) =>
  v === null || ["string", "number", "boolean"].includes(typeof v);

// An extraction schema often wraps its rows in one key ({ books: [...] }); the rows are the result.
function unwrapList(value: unknown): unknown {
  if (!isRecord(value)) return value;
  const values = Object.values(value);
  return values.length === 1 &&
    Array.isArray(values[0]) &&
    values[0].every(isRecord)
    ? values[0]
    : value;
}

/** What a finished step produced, shaped for the Doc: a table, field/value pairs, or one explorer. Null when nothing. */
export function docProduced(block: WorkflowRunBlock): DocProduced | null {
  const value = unwrapList(producedValue(block));
  if (isEmpty(value)) return null;
  if (
    Array.isArray(value) &&
    value.every((row) => isRecord(row) && Object.keys(row).length > 0)
  ) {
    const rows = value as Array<Record<string, unknown>>;
    const columns: string[] = [];
    for (const row of rows) {
      for (const key of Object.keys(row)) {
        if (!columns.includes(key)) columns.push(key);
      }
    }
    return {
      kind: "table",
      columns,
      rows: rows.slice(0, TABLE_ROW_CAP),
      moreRows: Math.max(0, rows.length - TABLE_ROW_CAP),
    };
  }
  if (isRecord(value) && Object.values(value).every(isScalar)) {
    return { kind: "pairs", pairs: Object.entries(value) };
  }
  return { kind: "json", value };
}

/** The full reason, when it says more than the one-line headline already does. */
export function docFailureDetail(block: WorkflowRunBlock): string | null {
  const raw = block.failure_reason?.trim();
  const headline = blockFailureHeadline(block);
  if (!raw || !headline || headline === NO_START_PAGE_TEXT) return null;
  const detail = formatFailureReason(raw).detail;
  if (!detail) return null;
  const full = plainFailureText(detail);
  return full && full !== headline ? full : null;
}

export function docFailureGuidance(block: WorkflowRunBlock): string | null {
  return describeCodeBlockFailure(block)?.guidance ?? null;
}

/** Steps whose "returned nothing" is worth saying: they exist to hand a value on. */
export function returnsAValue(block: WorkflowRunBlock): boolean {
  return block.block_type === "code" || block.block_type === "extraction";
}
