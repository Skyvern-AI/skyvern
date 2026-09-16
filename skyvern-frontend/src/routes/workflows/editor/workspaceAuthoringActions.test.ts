import { describe, expect, it, vi } from "vitest";

import {
  applySopResultAtCurrentAppend,
  resolveWorkspaceAuthoringActionAvailability,
} from "./workspaceAuthoringActions";

describe("workspace authoring actions", () => {
  it("applies an SOP at the current workflow tail when conversion finishes", () => {
    let nodes = [
      { id: "a", type: "task" },
      { id: "adder", type: "nodeAdder" },
    ];
    let edges = [{ source: "a", target: "adder" }];
    const setRecordedBlocks = vi.fn();
    const result = { blocks: [], parameters: [] };

    const applyResult = () =>
      applySopResultAtCurrentAppend({
        result,
        getNodes: () => nodes,
        getEdges: () => edges,
        setRecordedBlocks,
      });

    nodes = [
      { id: "a", type: "task" },
      { id: "b", type: "task" },
      { id: "adder", type: "nodeAdder" },
    ];
    edges = [
      { source: "a", target: "b" },
      { source: "b", target: "adder" },
    ];
    applyResult();

    expect(setRecordedBlocks).toHaveBeenCalledWith(result, {
      previous: "b",
      next: "adder",
      parent: undefined,
      connectingEdgeType: "default",
    });
  });

  it("makes SOP upload and task recording mutually exclusive", () => {
    expect(
      resolveWorkspaceAuthoringActionAvailability({
        browserReady: true,
        isGlobalWorkflow: false,
        isWorkflowDeleted: false,
        hasActiveRun: false,
        isComparing: false,
        isEditingYaml: false,
        hasFinallyBlock: false,
        isRecording: false,
        isUploadingSOP: true,
      }),
    ).toMatchObject({ canUploadSOP: false, canRecordTask: false });

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        browserReady: true,
        isGlobalWorkflow: false,
        isWorkflowDeleted: false,
        hasActiveRun: false,
        isComparing: false,
        isEditingYaml: false,
        hasFinallyBlock: false,
        isRecording: true,
        isUploadingSOP: false,
      }),
    ).toMatchObject({ canUploadSOP: false, canRecordTask: false });
  });

  it("blocks append-only actions on global workflows and after a finally block", () => {
    const base = {
      browserReady: true,
      isWorkflowDeleted: false,
      hasActiveRun: false,
      isComparing: false,
      isEditingYaml: false,
      isRecording: false,
      isUploadingSOP: false,
    };

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isGlobalWorkflow: true,
        hasFinallyBlock: false,
      }),
    ).toMatchObject({
      canUploadSOP: false,
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
      canUploadSOP: false,
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
      isRecording: false,
      isUploadingSOP: false,
    };

    expect(
      resolveWorkspaceAuthoringActionAvailability({
        ...base,
        isWorkflowDeleted: true,
        hasActiveRun: false,
      }),
    ).toEqual({
      canUploadSOP: false,
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
      canUploadSOP: true,
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
      canUploadSOP: false,
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
      canUploadSOP: false,
      canRecordTask: false,
      unavailableReason: "Switch to Visual mode before adding workflow steps",
    });
  });
});
