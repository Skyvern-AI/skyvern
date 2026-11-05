import { describe, expect, it } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../types/workflowRunTypes";
import { formatBlockDuration, getBlockResultSummary } from "./blockResult";

function block(overrides: Partial<WorkflowRunBlock>): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wfrb_1",
    workflow_run_id: "wr_1",
    parent_workflow_run_block_id: null,
    block_type: "extraction",
    label: "get_invoices",
    status: Status.Completed,
    duration: 9,
    output: null,
    ...overrides,
  } as WorkflowRunBlock;
}

describe("getBlockResultSummary", () => {
  it("counts extracted items for a completed extraction", () => {
    const b = block({ output: { extracted_information: [{}, {}, {}] } });
    expect(getBlockResultSummary(b)).toBe("Found 3 items");
    expect(formatBlockDuration(b)).toBe("9s");
  });

  it("says nothing extra for a failed block", () => {
    const b = block({
      status: Status.Failed,
      output: { extracted_information: [{}] },
    });
    expect(getBlockResultSummary(b)).toBeNull();
  });

  it("counts downloaded files", () => {
    const b = block({
      block_type: "file_download",
      output: { downloaded_file_urls: ["https://x/a.pdf", "https://x/b.pdf"] },
    });
    expect(getBlockResultSummary(b)).toBe("Downloaded 2 files");
  });

  it("keeps the web search summary the run page shows", () => {
    const b = block({
      block_type: "web_search",
      output: { total_count: 1 },
    });
    expect(getBlockResultSummary(b)).toBe("1 result");
  });
});
