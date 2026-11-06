import { describe, expect, it } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { docProduced, TABLE_ROW_CAP } from "./docStepProduced";

const block = (output: unknown, block_type = "extraction") =>
  ({ block_type, status: Status.Completed, output }) as WorkflowRunBlock;

describe("docProduced", () => {
  it("shapes extracted rows as a capped table, even when the schema wraps them in one key", () => {
    const rows = Array.from({ length: 8 }, (_, i) => ({
      title: `b${i}`,
      price: i,
    }));
    for (const extracted of [rows, { books: rows }]) {
      const produced = docProduced(block({ extracted_information: extracted }));
      expect(produced).toMatchObject({
        kind: "table",
        columns: ["title", "price"],
        moreRows: 8 - TABLE_ROW_CAP,
      });
    }
  });

  it("shapes a flat object as field/value pairs and nested data as one explorer", () => {
    expect(
      docProduced(block({ extracted_information: { total: 3 } }))?.kind,
    ).toBe("pairs");
    expect(
      docProduced(block({ extracted_information: { a: { b: 1 }, c: 2 } }))
        ?.kind,
    ).toBe("json");
  });

  it("says nothing for empty output, a bare success flag, a task's envelope or a loop", () => {
    expect(docProduced(block({ extracted_information: [] }))).toBeNull();
    expect(
      docProduced(
        block({ extracted_information: null, task_id: "tsk_1" }, "navigation"),
      ),
    ).toBeNull();
    expect(docProduced(block({ success: true }, "wait"))).toBeNull();
    expect(docProduced(block([{ a: 1 }], "for_loop"))).toBeNull();
  });
});
