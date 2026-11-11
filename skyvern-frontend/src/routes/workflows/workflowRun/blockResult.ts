import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../types/workflowRunTypes";

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
