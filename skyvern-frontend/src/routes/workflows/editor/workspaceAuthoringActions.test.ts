import { describe, expect, it } from "vitest";

import {
  blockLabelFor,
  demonstrationInsertionFromNodes,
  resolveAppendInsertionPoint,
  resolveWorkspaceAuthoringActionAvailability,
  sopAttachmentDraft,
} from "./workspaceAuthoringActions";

describe("workspace authoring actions", () => {
  it("appends after the last top-level block, not inside a container", () => {
    const nodes = [
      { id: "a", type: "task" },
      { id: "loop-adder", type: "nodeAdder", parentId: "loop" },
      { id: "b", type: "task" },
      { id: "adder", type: "nodeAdder" },
    ];
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "adder" },
    ];

    expect(resolveAppendInsertionPoint(nodes, edges)).toEqual({
      previous: "b",
      next: "adder",
      parent: undefined,
      connectingEdgeType: "default",
    });
  });

  it("blocks append-only actions on global workflows and after a finally block", () => {
    const base = {
      browserReady: true,
      isWorkflowDeleted: false,
      hasActiveRun: false,
      isComparing: false,
      isEditingYaml: false,
    };

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isGlobalWorkflow: true,
        hasFinallyBlock: false,
      }),
    ).toMatchObject({
      canRecordTask: false,
      unavailableReason: "Make a copy to edit this agent",
    });

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isGlobalWorkflow: false,
        hasFinallyBlock: true,
      }),
    ).toMatchObject({
      canRecordTask: false,
      unavailableReason:
        "The finally block must remain last. Add steps above it in the editor.",
    });
  });

  it("blocks deleted workflows and recording against an active run browser", () => {
    const base = {
      browserReady: true,
      isGlobalWorkflow: false,
      hasFinallyBlock: false,
      isComparing: false,
      isEditingYaml: false,
    };

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isWorkflowDeleted: true,
        hasActiveRun: false,
      }),
    ).toEqual({
      canRecordTask: false,
      unavailableReason: "This agent is deleted and view-only",
    });

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isWorkflowDeleted: false,
        hasActiveRun: true,
      }),
    ).toEqual({
      canRecordTask: false,
      unavailableReason: "Stop the active run before recording a task",
    });

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isWorkflowDeleted: false,
        hasActiveRun: false,
        isComparing: true,
      }),
    ).toEqual({
      canRecordTask: false,
      unavailableReason: "Exit history comparison to edit this agent",
    });

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isWorkflowDeleted: false,
        hasActiveRun: false,
        isComparing: false,
        isEditingYaml: true,
      }),
    ).toEqual({
      canRecordTask: false,
      unavailableReason: "Switch to Visual mode before adding workflow steps",
    });
  });
});

describe("demonstrationInsertionFromNodes", () => {
  it("uses adjacent workflow labels for the insertion", () => {
    const nodes = [
      { id: "first", type: "task", data: { label: "first_step" } },
      { id: "second", type: "task", data: { label: "second_step" } },
    ];

    expect(
      demonstrationInsertionFromNodes(nodes, {
        previous: "first",
        next: "second",
        parent: undefined,
      }),
    ).toEqual({
      after_label: "first_step",
      before_label: "second_step",
    });
  });

  it("refuses a parent-only insertion inside an empty container", () => {
    const nodes = [
      { id: "start", type: "start", parentId: "loop" },
      { id: "adder", type: "nodeAdder", parentId: "loop" },
    ];

    expect(
      demonstrationInsertionFromNodes(nodes, {
        previous: "start",
        next: "adder",
        parent: "loop",
      }),
    ).toBeNull();
  });
});

describe("sopAttachmentDraft", () => {
  it("names the container for an SOP added inside an empty loop", () => {
    const nodes = [
      { id: "loop", type: "loop", data: { label: "each_invoice" } },
      { id: "start", type: "start", parentId: "loop" },
      { id: "adder", type: "nodeAdder", parentId: "loop" },
    ];
    const insertionPoint = { previous: "start", next: "adder", parent: "loop" };

    expect(
      sopAttachmentDraft(
        demonstrationInsertionFromNodes(nodes, insertionPoint),
        blockLabelFor(nodes, insertionPoint.parent),
      ),
    ).toBe(
      "Add the steps from the attached SOP inside the each_invoice block.",
    );
  });
});
