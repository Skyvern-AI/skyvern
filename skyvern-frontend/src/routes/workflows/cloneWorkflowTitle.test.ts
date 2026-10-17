import { describe, expect, it } from "vitest";

import {
  getClonedWorkflowTitle,
  WORKFLOW_TITLE_MAX_LENGTH,
} from "./cloneWorkflowTitle";

describe("getClonedWorkflowTitle", () => {
  it("keeps copied titles within the workflow title limit", () => {
    expect(getClonedWorkflowTitle("Short title")).toBe("Copy of Short title");

    const title = getClonedWorkflowTitle(
      "🧭".repeat(WORKFLOW_TITLE_MAX_LENGTH),
    );

    expect([...title]).toHaveLength(WORKFLOW_TITLE_MAX_LENGTH);
    expect(title.startsWith("Copy of ")).toBe(true);
  });
});
