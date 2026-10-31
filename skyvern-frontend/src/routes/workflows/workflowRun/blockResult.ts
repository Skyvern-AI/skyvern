import { Status } from "@/api/types";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import {
  getBlockOutputDisplayValue,
  gmailSendOutcomeSummary,
  readGmailSendOutput,
  shouldShowExtractedInformation,
  type WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { WorkflowBlockTypes } from "../types/workflowTypes";

export function getWebSearchResultSummary(
  block: WorkflowRunBlock,
): string | null {
  const output = block.output;
  if (
    block.block_type !== "web_search" ||
    block.status !== Status.Completed ||
    !output ||
    typeof output !== "object" ||
    Array.isArray(output) ||
    !("total_count" in output) ||
    typeof output.total_count !== "number"
  ) {
    return null;
  }

  const count = output.total_count;
  const summary = `${count} ${count === 1 ? "result" : "results"}`;
  if ("prompt_output" in output && Array.isArray(output.prompt_output)) {
    let itemCount = output.prompt_output.length;
    const lastItem: unknown = output.prompt_output[itemCount - 1];
    if (
      lastItem &&
      typeof lastItem === "object" &&
      "truncated" in lastItem &&
      lastItem.truncated === true &&
      "reason" in lastItem &&
      lastItem.reason === "exceeded_max_run_response_value_size"
    ) {
      if (
        !("original_count" in lastItem) ||
        typeof lastItem.original_count !== "number"
      ) {
        return summary;
      }
      itemCount = lastItem.original_count;
    }
    return `${summary} · Prompt returned ${itemCount} ${itemCount === 1 ? "item" : "items"}`;
  }
  return summary;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * The one-line outcome of a finished block, shared by the run page step list and
 * the Doc's per-step result. Null when nothing useful can be said beyond status.
 */
export function getBlockResultSummary(block: WorkflowRunBlock): string | null {
  if (block.block_type === WorkflowBlockTypes.SendEmail) {
    const gmail = readGmailSendOutput(block.output);
    if (gmail) return gmailSendOutcomeSummary(gmail);
  }
  const webSearch = getWebSearchResultSummary(block);
  if (webSearch) return webSearch;
  if (block.status !== Status.Completed) return null;
  if (shouldShowExtractedInformation(block)) {
    const extracted = getBlockOutputDisplayValue(block);
    if (Array.isArray(extracted))
      return `Found ${plural(extracted.length, "item")}`;
    if (extracted && typeof extracted === "object") {
      return `Found ${plural(Object.keys(extracted).length, "field")}`;
    }
  }
  const files = blockDownloadedFileCount(block);
  if (files > 0) return `Downloaded ${plural(files, "file")}`;
  return null;
}

export function blockDownloadedFileCount(block: WorkflowRunBlock): number {
  const output = block.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) return 0;
  const urls = (output as Record<string, unknown>).downloaded_file_urls;
  return Array.isArray(urls) ? urls.length : 0;
}

export function formatBlockDuration(block: WorkflowRunBlock): string | null {
  return block.duration !== null && block.duration !== undefined
    ? formatDuration(toDuration(block.duration))
    : null;
}
