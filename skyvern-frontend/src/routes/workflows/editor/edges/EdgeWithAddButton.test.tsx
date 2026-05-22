// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { EdgeProps } from "@xyflow/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DebugStoreContext } from "@/store/DebugStoreContext";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { EdgeWithAddButton } from "./EdgeWithAddButton";

const useNodesMock = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");

  return {
    ...actual,
    BaseEdge: () => null,
    EdgeLabelRenderer: ({ children }: { children: ReactNode }) => (
      <>{children}</>
    ),
    getBezierPath: () => ["M 0 0", 0, 0],
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

function renderEdge(props: Partial<EdgeProps> = {}) {
  return render(
    <DebugStoreContext.Provider value={{ isDebugMode: false }}>
      <EdgeWithAddButton
        {...({
          id: "edge",
          markerEnd: undefined,
          source: "source",
          sourcePosition: "bottom",
          sourceX: 0,
          sourceY: 0,
          target: "target",
          targetPosition: "top",
          targetX: 0,
          targetY: 0,
          ...props,
        } as EdgeProps)}
      />
    </DebugStoreContext.Provider>,
  );
}

describe("EdgeWithAddButton", () => {
  beforeEach(() => {
    useSettingsStore.setState(initialSettings, true);
    useRecordingStore.setState(initialRecording, true);
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: false,
        content: "parameters",
      },
    });

    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useRecordingStore.setState({ isRecording: false });
    useNodesMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps the loop as parent and inherits branch context for nested-loop edge inserts", () => {
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
        id: "source",
        parentId: "loop",
        type: "start",
        data: {},
      },
    ]);

    renderEdge({ source: "source", target: "target" });

    fireEvent.click(screen.getByRole("button"));

    expect(useWorkflowPanelStore.getState().workflowPanelState).toMatchObject({
      active: true,
      content: "nodeLibrary",
      data: {
        branchContext: {
          branchId: "branch-a",
          conditionalNodeId: "conditional",
        },
        next: "target",
        parent: "loop",
        previous: "source",
      },
    });
  });

  it("does not branch-scope the post-merge edge after a top-level conditional", () => {
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
    ]);

    renderEdge({ source: "conditional", target: "after-conditional" });

    fireEvent.click(screen.getByRole("button"));

    expect(useWorkflowPanelStore.getState().workflowPanelState).toMatchObject({
      active: true,
      content: "nodeLibrary",
      data: {
        branchContext: undefined,
        next: "after-conditional",
        parent: undefined,
        previous: "conditional",
      },
    });
  });
});
