import { describe, expect, it } from "vitest";

import { Status } from "@/api/types";
import {
  getBlockOutputDisplayValue,
  getExtractedInformationDisplayValue,
  shouldShowExtractedInformation,
} from "./workflowRunTypes";

describe("workflow run output display helpers", () => {
  it("only labels task-variant output as extracted information when the wrapper field is present", () => {
    const block = {
      block_type: "task_v2",
      status: Status.Completed,
      output: { result: "plain task output" },
    } as const;

    expect(shouldShowExtractedInformation(block)).toBe(false);
    expect(getBlockOutputDisplayValue(block)).toEqual({
      result: "plain task output",
    });
  });

  it("preserves empty extracted information payloads as extracted information", () => {
    const block = {
      block_type: "code",
      status: Status.Completed,
      output: { extracted_information: null },
    } as const;

    expect(shouldShowExtractedInformation(block)).toBe(true);
    expect(getExtractedInformationDisplayValue(block)).toBeNull();
  });
});
