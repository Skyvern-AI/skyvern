import { describe, expect, it } from "vitest";
import {
  DRAFT_WORKFLOW_PERMANENT_ID,
  buildDraftWorkflowApiResponse,
  isDraftWorkflowPermanentId,
} from "./draftWorkflow";

describe("draftWorkflow", () => {
  it("detects the draft workflow permanent id", () => {
    expect(isDraftWorkflowPermanentId("new")).toBe(true);
    expect(isDraftWorkflowPermanentId("wpid_abc")).toBe(false);
    expect(isDraftWorkflowPermanentId(undefined)).toBe(false);
  });

  it("keeps the default title when overrides are undefined", () => {
    const workflow = buildDraftWorkflowApiResponse({
      title: undefined,
      run_with: undefined,
      folder_id: undefined,
    });

    expect(workflow.title).toBe("New Agent");
  });

  it("builds an in-memory workflow payload without persisting", () => {
    const workflow = buildDraftWorkflowApiResponse({
      title: "Handoff title",
      folder_id: "fld_1",
      run_with: "code",
    });

    expect(workflow.workflow_permanent_id).toBe(DRAFT_WORKFLOW_PERMANENT_ID);
    expect(workflow.title).toBe("Handoff title");
    expect(workflow.folder_id).toBe("fld_1");
    expect(workflow.run_with).toBe("code");
    expect(workflow.workflow_definition.blocks).toEqual([]);
  });
});
