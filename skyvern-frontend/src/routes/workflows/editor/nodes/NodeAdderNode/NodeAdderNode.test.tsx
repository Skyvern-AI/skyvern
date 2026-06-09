// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Edge, NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DebugStoreContext } from "@/store/DebugStoreContext";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";

import { NodeAdderNode } from "./NodeAdderNode";
import type { NodeAdderNode as NodeAdderNodeType } from "./types";

const useEdgesMock = vi.fn();
const useNodesMock = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");

  return {
    ...actual,
    Handle: () => null,
    useEdges: () => useEdgesMock(),
    useNodes: () => useNodesMock(),
  };
});

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );

  return {
    ...actual,
    useParams: () => ({ workflowPermanentId: "wpid_test" }),
  };
});

vi.mock("@/routes/browserSessions/hooks/useProcessRecordingMutation", () => ({
  useProcessRecordingMutation: () => ({
    isPending: false,
    mutate: vi.fn(),
  }),
}));

vi.mock("@/routes/workflows/hooks/useSopToBlocksMutation", () => ({
  useSopToBlocksMutation: () => ({
    cancel: vi.fn(),
    isPending: false,
    mutate: vi.fn(),
  }),
}));

const initialSettings = useSettingsStore.getState();
const initialRecording = useRecordingStore.getState();
const initialWorkflowSettings = useWorkflowSettingsStore.getState();

function renderNodeAdder(props: Partial<NodeProps<NodeAdderNodeType>>) {
  return render(
    <DebugStoreContext.Provider value={{ isDebugMode: false }}>
      <NodeAdderNode
        {...({
          id: "adder",
          parentId: "loop",
          ...props,
        } as NodeProps<NodeAdderNodeType>)}
      />
    </DebugStoreContext.Provider>,
  );
}

describe("NodeAdderNode", () => {
  beforeEach(() => {
    useSettingsStore.setState(initialSettings, true);
    useRecordingStore.setState(initialRecording, true);
    useWorkflowSettingsStore.setState(initialWorkflowSettings, true);
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: false,
        content: "parameters",
      },
    });

    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useRecordingStore.setState({ isRecording: false });
    useWorkflowSettingsStore.setState({ finallyBlockLabel: null });
    useEdgesMock.mockReset();
    useNodesMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps the loop as parent without branch context when adding the first block in a nested loop", () => {
    // SKY-10719: adding a block into a loop nested in a conditional keeps the
    // loop as parent with no branch context (loop children are not branch members).
    const edges: Array<Edge> = [
      {
        id: "edge",
        source: "start",
        target: "adder",
      },
    ];

    useEdgesMock.mockReturnValue(edges);
    useNodesMock.mockReturnValue([
      {
        id: "conditional",
        type: "conditional",
        data: {
          activeBranchId: "branch-a",
          branches: [{ id: "branch-a" }],
          label: "conditional",
          mergeLabel: null,
        },
      },
      {
        id: "loop",
        parentId: "conditional",
        type: "loop",
        data: {
          conditionalBranchId: "branch-a",
          conditionalLabel: "conditional",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
          label: "block_8",
        },
      },
      {
        id: "start",
        parentId: "loop",
        type: "start",
        data: {},
      },
    ]);

    renderNodeAdder({});

    fireEvent.click(screen.getByTestId("node-adder-button"));

    const state = useWorkflowPanelStore.getState().workflowPanelState;
    expect(state).toMatchObject({
      active: true,
      content: "nodeLibrary",
      data: {
        next: "adder",
        parent: "loop",
        previous: "start",
      },
    });
    expect(state.data?.branchContext).toBeUndefined();
  });
});
