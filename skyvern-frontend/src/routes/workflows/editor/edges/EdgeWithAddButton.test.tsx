// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { EdgeProps } from "@xyflow/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  createYamlCommitOwner,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { DebugStoreContext } from "@/store/DebugStoreContext";
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

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

function queryWrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

const initialSettings = useSettingsStore.getState();

function renderEdge(
  props: Partial<EdgeProps> = {},
  { debug = false }: { debug?: boolean } = {},
) {
  return render(
    <DebugStoreContext.Provider
      value={{ isDebugMode: debug, blockRunsEnabled: false }}
    >
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
    { wrapper: queryWrapper },
  );
}

describe("EdgeWithAddButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useSettingsStore.setState(initialSettings, true);
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: false,
        content: "parameters",
      },
    });

    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useNodesMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    useCopilotActionStore.setState({ stageAttachment: null });
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  it("stages a picked PDF in Copilot with a draft naming the block after it", () => {
    useNodesMock.mockReturnValue([
      { id: "source", type: "start", data: {} },
      { id: "target", type: "task", data: { label: "download" } },
    ]);
    const { container } = renderEdge({}, { debug: true });
    const uploadItem = () =>
      screen.getByText("Upload SOP").parentElement!.querySelector("button")!;
    expect(uploadItem().disabled).toBe(true);

    const stage = vi.fn();
    act(() => useCopilotActionStore.setState({ stageAttachment: stage }));
    expect(uploadItem().disabled).toBe(false);
    const file = new File(["pdf"], "steps.pdf", { type: "application/pdf" });
    fireEvent.change(
      container.querySelector<HTMLInputElement>('input[type="file"]')!,
      { target: { files: [file] } },
    );
    expect(stage).toHaveBeenCalledWith(
      file,
      "Add the steps from the attached SOP before the download block.",
    );
  });

  it("keeps the loop as parent without branch context for nested-loop edge inserts", () => {
    // SKY-10719: a block inside a loop is a plain loop child even when the loop
    // lives in a conditional; keep the loop as parent with no branch context.
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

    const state = useWorkflowPanelStore.getState().workflowPanelState;
    expect(state).toMatchObject({
      active: true,
      content: "nodeLibrary",
      data: {
        next: "target",
        parent: "loop",
        previous: "source",
      },
    });
    expect(state.data?.branchContext).toBeUndefined();
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
