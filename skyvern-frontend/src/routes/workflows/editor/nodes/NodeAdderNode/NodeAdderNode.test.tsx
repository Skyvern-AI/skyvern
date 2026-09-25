// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import type { Edge, NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import type { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import {
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  beginYamlCommit,
  createYamlCommitOwner,
  registerEditorOwner,
  unregisterEditorOwner,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
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

const recordingMutation = vi.hoisted(() => ({
  onSuccess: undefined as Parameters<
    typeof useProcessRecordingMutation
  >[0]["onSuccess"],
}));
vi.mock("@/routes/browserSessions/hooks/useProcessRecordingMutation", () => ({
  useProcessRecordingMutation: (
    options: Parameters<typeof useProcessRecordingMutation>[0],
  ) => {
    recordingMutation.onSuccess = options.onSuccess;
    return { isPending: false, mutate: vi.fn() };
  },
}));

const uploadMocks = vi.hoisted(() => ({
  getClient: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}));
vi.mock("@/api/AxiosClient", () => ({ getClient: uploadMocks.getClient }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));
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
const initialRecording = useRecordingStore.getState();
const initialWorkflowSettings = useWorkflowSettingsStore.getState();

function renderNodeAdder(
  props: Partial<NodeProps<NodeAdderNodeType>>,
  { debug = false }: { debug?: boolean } = {},
) {
  return render(
    <DebugStoreContext.Provider
      value={{ isDebugMode: debug, blockRunsEnabled: false }}
    >
      <NodeAdderNode
        {...({
          id: "adder",
          parentId: "loop",
          ...props,
        } as NodeProps<NodeAdderNodeType>)}
      />
    </DebugStoreContext.Provider>,
    { wrapper: queryWrapper },
  );
}

describe("NodeAdderNode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useRecordedBlocksStore.getState().clearRecordedBlocks();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    uploadMocks.getClient.mockResolvedValue({
      post: uploadMocks.post,
      put: uploadMocks.put,
    });
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
    useRecordedBlocksStore.getState().clearRecordedBlocks();
    useRecordingStore.setState(initialRecording, true);
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  it.each([true, false])(
    "publishes recording blocks only for a live owner: %s",
    (live) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      useNodesMock.mockReturnValue([]);
      useEdgesMock.mockReturnValue([]);
      renderNodeAdder({});
      if (!live) {
        act(() => {
          unregisterEditorOwner(owner);
          registerEditorOwner(createYamlCommitOwner("wpid-other"));
        });
      }
      act(() =>
        recordingMutation.onSuccess?.(
          { recordingId: "br-1", blocks: [], parameters: [] },
          owner,
        ),
      );
      expect(useRecordedBlocksStore.getState().owner).toBe(live ? owner : null);
      expect(useRecordedBlocksStore.getState().blocks).toEqual(
        live ? [] : null,
      );
    },
  );

  it.each(["copilot", "yaml"])(
    "refuses canvas recording during %s ownership and releases it on abort",
    async (lock) => {
      useEdgesMock.mockReturnValue([
        { id: "edge", source: "start", target: "adder" },
      ]);
      useNodesMock.mockReturnValue([{ id: "start", type: "start", data: {} }]);
      useSettingsStore.getState().setIsUsingABrowser(true);
      const owner = createYamlCommitOwner("wpid_test");
      const token = lock === "copilot" ? beginCopilotAcceptance() : null;
      if (lock === "yaml") expect(beginYamlCommit(owner)).toBe(true);
      renderNodeAdder({}, { debug: true });
      await act(async () => fireEvent.click(screen.getByText("Record Task")));
      expect(useRecordingStore.getState().isRecording).toBe(false);
      act(() => {
        if (token) finishCopilotAcceptance(token);
        else finishYamlCommit(owner);
      });
      await act(async () => fireEvent.click(screen.getByText("Record Task")));
      expect(useRecordingStore.getState().isRecording).toBe(true);
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );
      const conversational = beginCopilotAcceptance();
      expect(conversational).not.toBeNull();
      finishCopilotAcceptance(conversational!);
      act(() => useRecordingStore.getState().reset());
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        false,
      );
      const reservation = beginCopilotAcceptance();
      expect(reservation).not.toBeNull();
      finishCopilotAcceptance(reservation!);
    },
  );

  it.each(["copilot", "yaml"])(
    "refuses a canvas upload while %s owns the editor",
    async (lock) => {
      useEdgesMock.mockReturnValue([
        { id: "edge", source: "start", target: "adder" },
      ]);
      useNodesMock.mockReturnValue([{ id: "start", type: "start", data: {} }]);
      const owner = createYamlCommitOwner("wpid_test");
      const token = lock === "copilot" ? beginCopilotAcceptance() : null;
      if (lock === "yaml") expect(beginYamlCommit(owner)).toBe(true);
      const { container } = renderNodeAdder({});
      const input =
        container.querySelector<HTMLInputElement>('input[type="file"]')!;
      await act(async () =>
        fireEvent.change(input, {
          target: {
            files: [
              new File(["pdf"], "steps.pdf", { type: "application/pdf" }),
            ],
          },
        }),
      );
      expect(uploadMocks.getClient).not.toHaveBeenCalled();
      expect(uploadMocks.post).not.toHaveBeenCalled();
      expect(useRecordedBlocksStore.getState().blocks).toBeNull();
      if (token) finishCopilotAcceptance(token);
      else finishYamlCommit(owner);
    },
  );

  it("holds authoring through upload settlement until generated blocks are applied", async () => {
    useEdgesMock.mockReturnValue([
      { id: "edge", source: "start", target: "adder" },
    ]);
    useNodesMock.mockReturnValue([{ id: "start", type: "start", data: {} }]);
    let resolveUpload!: (value: unknown) => void;
    uploadMocks.post.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const { container } = renderNodeAdder({});
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, {
      target: { files: [new File(["pdf"], "steps.pdf")] },
    });
    expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
      true,
    );
    expect(beginCopilotAcceptance()).toBeNull();
    expect(beginYamlCommit(createYamlCommitOwner("wpid_test"))).toBe(false);
    await waitFor(() => expect(uploadMocks.post).toHaveBeenCalledTimes(1));
    await act(async () =>
      resolveUpload({
        data: {
          blocks: [
            {
              block_type: "goto_url",
              label: "uploaded",
              url: "https://example.com",
            },
          ],
          parameters: [],
        },
      }),
    );
    expect(useRecordedBlocksStore.getState().blocks).toHaveLength(1);
    expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
      true,
    );
    act(() => useRecordedBlocksStore.getState().clearRecordedBlocks());
    expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
      false,
    );
    const token = beginCopilotAcceptance();
    expect(token).not.toBeNull();
    finishCopilotAcceptance(token!);
  });

  it("releases authoring after two immediate upload failures and permits Save", async () => {
    useEdgesMock.mockReturnValue([
      { id: "edge", source: "start", target: "adder" },
    ]);
    useNodesMock.mockReturnValue([{ id: "start", type: "start", data: {} }]);
    uploadMocks.getClient.mockRejectedValue(
      new Error("credentials unavailable"),
    );
    const { container } = renderNodeAdder({});
    const input =
      container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(["pdf"], "steps.pdf");
    await act(async () => {
      for (let attempt = 1; attempt <= 2; attempt++) {
        fireEvent.change(input, { target: { files: [file] } });
        expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
          true,
        );
        await vi.waitFor(() => {
          expect(uploadMocks.getClient).toHaveBeenCalledTimes(attempt);
          expect(
            useWorkflowYamlEditorStore.getState().authoringInProgress,
          ).toBe(false);
        });
      }
    });
    expect(uploadMocks.post).not.toHaveBeenCalled();
    uploadMocks.getClient.mockResolvedValue({ put: uploadMocks.put });
    const saveData = {
      title: "Draft",
      description: null,
      blocks: [],
      parameters: [],
      workflowDefinitionVersion: 1,
      settings: { proxyLocation: "RESIDENTIAL", runWith: "agent" },
      workflow: {
        workflow_permanent_id: "wpid_test",
        workflow_definition: { version: 1, blocks: [], parameters: [] },
      },
    } as unknown as WorkflowSaveData;
    useWorkflowHasChangesStore.setState({
      getSaveData: () => saveData,
      hasChanges: true,
    });
    uploadMocks.put.mockResolvedValue({
      data: { ...saveData.workflow, title: "Draft", description: null },
    });
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    const { result } = renderHook(() => useWorkflowSave(), {
      wrapper: queryWrapper,
    });
    await act(async () => result.current.mutateAsync(undefined));
    expect(uploadMocks.put).toHaveBeenCalledTimes(1);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
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

  it("shows processing instead of a second stop action after Done", () => {
    useEdgesMock.mockReturnValue([
      { id: "edge", source: "start", target: "adder" },
    ]);
    useNodesMock.mockReturnValue([
      { id: "start", parentId: "loop", type: "start", data: {} },
    ]);
    useSettingsStore.getState().setIsUsingABrowser(true);
    useRecordingStore.setState({
      isRecording: true,
      finishRequested: true,
    });
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: false,
        content: "nodeLibrary",
        data: {
          previous: "start",
          next: "adder",
          parent: "loop",
          connectingEdgeType: "default",
        },
      },
    });

    renderNodeAdder({}, { debug: true });

    const busyControl =
      screen.getByTestId("node-adder-button").parentElement?.parentElement;
    expect(busyControl).toBeTruthy();
    fireEvent.mouseEnter(busyControl!);

    expect(screen.getAllByText("Processing...").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("Stop & generate workflow")).toHaveLength(0);

    fireEvent.click(busyControl!.firstElementChild!);
    expect(useRecordingStore.getState()).toMatchObject({
      isRecording: true,
      finishRequested: true,
    });
  });
});
