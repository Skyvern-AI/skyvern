// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryRouter, useLocation } from "react-router-dom";

import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useRecordingRefinementEvidenceStore } from "@/store/RecordingRefinementEvidenceStore";
import {
  useRecordingStore,
  type OptimisticStep,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { toast } from "@/components/ui/use-toast";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  beginCopilotAcceptance,
  beginSaveTransaction,
  createYamlCommitOwner,
  finishCopilotAcceptance,
  finishSaveTransaction,
  registerEditorOwner,
  unregisterEditorOwner,
  useWorkflowYamlEditorStore,
  type YamlCommitOwner,
} from "@/store/WorkflowYamlEditorStore";
import type { AppNode } from "@/routes/workflows/editor/nodes";
import { useWorkflowGraphState } from "@/routes/workflows/editor/workflowEditorUtils";
import { useApplyRecordedBlocks } from "@/routes/workflows/editor/recording/useApplyRecordedBlocks";

import { useProcessRecordingMutation } from "./useProcessRecordingMutation";

const mocks = vi.hoisted(() => ({
  captureRecordBrowser: vi.fn(),
  markRecordBrowserProcessed: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ post: mocks.post })),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(async () => "test-token"),
}));

vi.mock("@/util/recordBrowserTelemetry", () => ({
  captureRecordBrowser: mocks.captureRecordBrowser,
  markRecordBrowserProcessed: mocks.markRecordBrowserProcessed,
}));

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return (
    <MemoryRouter initialEntries={["/agents/wpid-1/edit"]}>
      <WorkflowPermanentIdContext.Provider value="wpid-1">
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      </WorkflowPermanentIdContext.Provider>
    </MemoryRouter>
  );
}

const draftStep: RecordingDraftStep = {
  step_id: "step-1",
  action_kind: "click",
  block_type: "action",
  label: "Click",
  status: "ready",
  editable_fields: [],
  parameters: [],
  parameter_keys: [],
};

const optimisticStep: OptimisticStep = {
  local_id: "optimistic-1",
  action_kind: "click",
  title: "Click",
  timestamp: 1,
};

describe("useProcessRecordingMutation telemetry", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(1_000);
    vi.clearAllMocks();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid-1"));
    useRecordedBlocksStore.getState().clearRecordedBlocks();
    useWorkflowParametersStore.setState({ parameters: [] });
    useRecordingRefinementEvidenceStore.setState({ armed: null });
    useRecordingStore.getState().reset();
    useRecordingStore.getState().setRecordingTransport("cdp");
    useRecordingStore.getState().setIsRecording(true);
    useRecordingStore.setState({ optimisticSteps: [optimisticStep] });
    useWorkflowHasChangesStore.setState({
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
    });
    mocks.captureRecordBrowser.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    useRecordingStore.getState().reset();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  it("reports successful processing with the recording transport and counters", async () => {
    mocks.post.mockResolvedValue({ data: { blocks: [], parameters: [] } });
    vi.setSystemTime(3_500);
    useRecordingStore.getState().setIsRecording(false);
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mocks.captureRecordBrowser).toHaveBeenCalledWith(
      "record_browser.finished",
      {
        transport: "cdp",
        duration_ms: 2_500,
        event_count: 0,
        optimistic_step_count: 1,
      },
    );
  });

  it("arms the refine_recording copilot turn with the response evidence packet", async () => {
    const evidence = {
      schema_version: 1,
      recording: { browser_session_id: "pbs-1" },
      actions: [{ action_id: "a001" }, { action_id: "a002" }],
      deleted_action_ids: [],
      truncated_action_count: 0,
      provenance: { source: "browser_recording" },
    };
    mocks.post.mockResolvedValue({
      data: { blocks: [{ label: "code" }], parameters: [], evidence },
    });
    const { result } = renderHook(
      () => ({
        mutation: useProcessRecordingMutation({ browserSessionId: "pbs-1" }),
        locationState: useLocation().state,
      }),
      { wrapper },
    );

    act(() => result.current.mutation.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.mutation.isSuccess).toBe(true));
    const armed = useRecordingRefinementEvidenceStore.getState().armed;
    expect(armed?.evidence).toEqual(evidence);
    // Route state carries only the nonce, so it has to name the packet the copilot
    // turn will take back out of the store.
    await waitFor(() =>
      expect(result.current.locationState).toEqual({
        copilotAction: { kind: "refine_recording", nonce: armed!.nonce },
      }),
    );
    expect(
      useRecordingRefinementEvidenceStore.getState().take(armed!.nonce),
    ).toEqual(evidence);
  });

  it("sends the recording correlation ids used by live interpretation", async () => {
    mocks.post.mockResolvedValue({ data: { blocks: [], parameters: [] } });
    useRecordingStore.setState({
      recordingAttemptId: "attempt-1",
      interpretationSessionId: "interpretation-1",
    });
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mocks.post).toHaveBeenCalledWith(
      "/browser_sessions/pbs-1/process_recording",
      expect.objectContaining({
        code_first: true,
        supports_credential_tokens: true,
        recording_attempt_id: "attempt-1",
        interpretation_session_id: "interpretation-1",
      }),
    );
  });

  it("keeps the finalized recording identity available for a retry", async () => {
    mocks.post.mockRejectedValue(new Error("temporary processing failure"));
    useRecordingStore.setState({
      finishRequested: true,
      recordingAttemptId: "attempt-1",
      interpretationSessionId: "interpretation-1",
    });
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(useRecordingStore.getState()).toMatchObject({
      finishRequested: true,
      recordingAttemptId: "attempt-1",
      interpretationSessionId: "interpretation-1",
    });
  });

  it("keeps the durable recording id for the workflow save that follows", async () => {
    mocks.post.mockResolvedValue({
      data: {
        recording_id: "br-1",
        blocks: [{ block_type: "action", label: "click" }],
        parameters: [],
      },
    });
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("lands recorded blocks when an older backend omits the recording id", async () => {
    const onSuccess = vi.fn();
    mocks.post.mockResolvedValue({
      data: {
        blocks: [{ block_type: "action", label: "click" }],
        parameters: [],
      },
    });
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
          onSuccess,
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(onSuccess).toHaveBeenCalledWith(
      {
        recordingId: null,
        blocks: [{ block_type: "action", label: "click" }],
        parameters: [],
      },
      useWorkflowYamlEditorStore.getState().editorOwner,
    );
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBeNull();
  });

  it("does not process another recording before the pending one is saved", async () => {
    useWorkflowHasChangesStore.setState({
      pendingRecordingId: "br-pending",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    await expect(
      act(async () => result.current.mutateAsync({ draftSteps: [draftStep] })),
    ).rejects.toThrow("Save or discard the current workflow changes");
    expect(mocks.post).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
      "br-pending",
    );
  });

  it.each([
    ["wpid-1", "success"],
    ["wpid-1", "error"],
    ["wpid-2", "success"],
    ["wpid-2", "error"],
  ])(
    "resets the Done lifecycle after owner disposal to %s and a deferred %s",
    async (nextWorkflowId, outcome) => {
      vi.useRealTimers();
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      useRecordingStore.getState().requestFinish();
      useRecordingStore.setState({ workflowPermanentId: "wpid-1" });
      let resolveRequest!: (value: unknown) => void;
      let rejectRequest!: (error: Error) => void;
      mocks.post.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolveRequest = resolve;
            rejectRequest = reject;
          }),
      );
      const { result } = renderHook(
        () => useProcessRecordingMutation({ browserSessionId: "pbs-1" }),
        { wrapper },
      );
      act(() => result.current.mutate({ draftSteps: [draftStep] }));
      await waitFor(() => expect(mocks.post).toHaveBeenCalled());
      expect(useRecordingStore.getState()).toMatchObject({
        isRecording: true,
        finishRequested: true,
        isCommitting: true,
      });
      const nextOwner = createYamlCommitOwner(nextWorkflowId);
      act(() => {
        unregisterEditorOwner(owner);
        registerEditorOwner(nextOwner);
      });
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );

      expect(beginSaveTransaction(nextOwner)).toBe(false);
      expect(beginCopilotAcceptance()).toBeNull();

      await act(async () => {
        if (outcome === "error") rejectRequest(new Error("Processing failed"));
        else resolveRequest({ data: { blocks: [], parameters: [] } });
      });
      await waitFor(() => expect(result.current.isPending).toBe(false));

      expect(useRecordingStore.getState()).toMatchObject({
        finishRequested: false,
        isRecording: false,
        isCommitting: false,
      });
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        false,
      );
      expect(beginSaveTransaction(nextOwner)).toBe(true);
      finishSaveTransaction(nextOwner);
      const copilotToken = beginCopilotAcceptance();
      expect(copilotToken).not.toBeNull();
      finishCopilotAcceptance(copilotToken!);
    },
  );

  it.each([
    ["commitToken", "success"],
    ["commitToken", "error"],
    ["workflowPermanentId", "success"],
    ["workflowPermanentId", "error"],
    ["recordingAttemptId", "success"],
    ["recordingAttemptId", "error"],
  ] as const)(
    "preserves a newer recording with a different %s after disposed %s",
    async (identityField, outcome) => {
      vi.useRealTimers();
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      useRecordingStore.getState().requestFinish();
      useRecordingStore.setState({ workflowPermanentId: "wpid-1" });
      let resolveRequest!: (value: unknown) => void;
      let rejectRequest!: (error: Error) => void;
      mocks.post.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolveRequest = resolve;
            rejectRequest = reject;
          }),
      );
      const { result } = renderHook(
        () => useProcessRecordingMutation({ browserSessionId: "pbs-1" }),
        { wrapper },
      );
      act(() => result.current.mutate({ draftSteps: [draftStep] }));
      await waitFor(() => expect(mocks.post).toHaveBeenCalled());
      act(() => {
        unregisterEditorOwner(owner);
        registerEditorOwner(createYamlCommitOwner("wpid-2"));
        if (identityField === "commitToken") {
          useRecordingStore.getState().setIsCommitting(true);
        } else {
          useRecordingStore.setState({ [identityField]: "new-recording" });
        }
      });
      const newerRecording = useRecordingStore.getState();

      await act(async () => {
        if (outcome === "error") rejectRequest(new Error("Processing failed"));
        else resolveRequest({ data: { blocks: [], parameters: [] } });
      });
      await waitFor(() => expect(result.current.isPending).toBe(false));

      expect(useRecordingStore.getState()).toBe(newerRecording);
      expect(useRecordingStore.getState()).toMatchObject({
        isRecording: true,
        finishRequested: true,
        isCommitting: true,
      });
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );
    },
  );

  it.each(["wpid-1", "wpid-2"])(
    "preserves the newer owner's committing flag for %s until its request settles",
    async (nextWorkflowId) => {
      vi.useRealTimers();
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      useRecordingStore.setState({ workflowPermanentId: "wpid-1" });
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      let workflowId = "wpid-1";
      const editorWrapper = ({ children }: { children: ReactNode }) => (
        <MemoryRouter initialEntries={["/agents/wpid-1/edit"]}>
          <WorkflowPermanentIdContext.Provider value={workflowId}>
            <QueryClientProvider client={client}>
              {children}
            </QueryClientProvider>
          </WorkflowPermanentIdContext.Provider>
        </MemoryRouter>
      );
      let resolveFirstRequest!: (value: unknown) => void;
      let rejectSecondRequest!: (error: Error) => void;
      mocks.post
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              resolveFirstRequest = resolve;
            }),
        )
        .mockImplementationOnce(
          () =>
            new Promise((_resolve, reject) => {
              rejectSecondRequest = reject;
            }),
        );
      const first = renderHook(
        () => useProcessRecordingMutation({ browserSessionId: "pbs-1" }),
        { wrapper: editorWrapper },
      );
      act(() => first.result.current.mutate({ draftSteps: [draftStep] }));
      await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(1));
      expect(useRecordingStore.getState().isCommitting).toBe(true);
      act(() => {
        unregisterEditorOwner(owner);
        registerEditorOwner(createYamlCommitOwner(nextWorkflowId));
        workflowId = nextWorkflowId;
        if (nextWorkflowId !== "wpid-1") {
          useRecordingStore.getState().setIsRecording(false);
          useRecordingStore.getState().setIsRecording(true, {
            workflowPermanentId: nextWorkflowId,
          });
        }
      });
      const second = renderHook(
        () => useProcessRecordingMutation({ browserSessionId: "pbs-1" }),
        { wrapper: editorWrapper },
      );
      act(() => second.result.current.mutate({ draftSteps: [draftStep] }));
      await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(2));
      expect(useRecordingStore.getState().isCommitting).toBe(true);

      await act(async () =>
        resolveFirstRequest({ data: { blocks: [], parameters: [] } }),
      );
      await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
      expect(second.result.current.isPending).toBe(true);
      expect(useRecordingStore.getState().isCommitting).toBe(true);

      await act(async () =>
        rejectSecondRequest(new Error("Processing failed")),
      );
      await waitFor(() => expect(second.result.current.isError).toBe(true));
      expect(useRecordingStore.getState().isCommitting).toBe(false);
      first.unmount();
      second.unmount();
      client.clear();
    },
  );

  it.each(["success", "empty success", "error", "empty error"])(
    "ignores deferred recording %s after navigation to another editor",
    async (outcome) => {
      vi.useRealTimers();
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      const client = new QueryClient({
        defaultOptions: { mutations: { retry: false } },
      });
      let workflowId = "wpid-1";
      const editorWrapper = ({ children }: { children: ReactNode }) => (
        <MemoryRouter initialEntries={["/agents/wpid-1/edit"]}>
          <WorkflowPermanentIdContext.Provider value={workflowId}>
            <QueryClientProvider client={client}>
              {children}
            </QueryClientProvider>
          </WorkflowPermanentIdContext.Provider>
        </MemoryRouter>
      );
      let resolveRequest!: (value: unknown) => void;
      let rejectRequest!: (error: Error) => void;
      mocks.post.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolveRequest = resolve;
            rejectRequest = reject;
          }),
      );
      const onSuccess = vi.fn((result, capturedOwner: YamlCommitOwner) => {
        useRecordedBlocksStore.getState().setRecordedBlocks(
          result,
          {
            previous: "a-start",
            next: "a-adder",
            connectingEdgeType: "default",
          },
          capturedOwner,
        );
        useRecordingStore.getState().setIsRecording(false);
      });
      const { result, rerender, unmount } = renderHook(
        () => {
          const graph = useWorkflowGraphState([], []);
          useApplyRecordedBlocks({
            enabled: true,
            nodes: graph.nodes,
            edges: graph.edges,
            doLayout: (nodes, edges) => {
              graph.setNodes(nodes);
              graph.setEdges(edges);
            },
          });
          return {
            graph,
            mutation: useProcessRecordingMutation({
              browserSessionId: "pbs-1",
              onSuccess,
            }),
            location: useLocation(),
          };
        },
        { wrapper: editorWrapper },
      );
      act(() => result.current.mutation.mutate({ draftSteps: [draftStep] }));
      await waitFor(() => expect(mocks.post).toHaveBeenCalled());
      act(() => {
        unregisterEditorOwner(owner);
        registerEditorOwner(createYamlCommitOwner("wpid-2"));
        workflowId = "wpid-2";
        result.current.graph.setNodes([
          { id: "b-start", type: "start", position: { x: 0, y: 0 }, data: {} },
          {
            id: "b-adder",
            type: "nodeAdder",
            position: { x: 0, y: 0 },
            data: {},
          },
        ] as AppNode[]);
        result.current.graph.setEdges([
          { id: "b-edge", source: "b-start", target: "b-adder" },
        ]);
        useWorkflowParametersStore.setState({
          parameters: [
            {
              key: "b_parameter",
              parameterType: "workflow",
              dataType: "string",
              defaultValue: "b",
              description: "",
            },
          ],
        });
        useRecordingStore.setState({
          isRecording: true,
          isCommitting: true,
          finishRequested: true,
          workflowPermanentId: "wpid-2",
          recordingAttemptId: "attempt-b",
          draftSteps: [draftStep],
        });
        useWorkflowHasChangesStore.setState({
          hasChanges: false,
          pendingRecordingId: "br-b",
          pendingRecordingWorkflowPermanentId: "wpid-2",
        });
        useRecordingRefinementEvidenceStore.getState().set({
          nonce: "b-evidence",
          evidence: {
            schema_version: 1,
            recording: { browser_session_id: "pbs-b" },
            actions: [],
            deleted_action_ids: [],
            truncated_action_count: 0,
            provenance: { source: "browser_recording" },
          },
        });
      });
      rerender();
      const graph = result.current.graph;
      const parameters = useWorkflowParametersStore.getState().parameters;
      const recording = useRecordingStore.getState();
      const pending = useWorkflowHasChangesStore.getState();
      const evidence = useRecordingRefinementEvidenceStore.getState().armed;
      const location = result.current.location;
      vi.mocked(toast).mockClear();
      mocks.captureRecordBrowser.mockClear();
      await act(async () => {
        if (outcome.endsWith("error"))
          rejectRequest(
            new Error(
              outcome === "empty error"
                ? "FAIL-QUIET:NO-EVENTS"
                : "Processing failed",
            ),
          );
        else
          resolveRequest({
            data: {
              recording_id: "br-a",
              blocks:
                outcome === "empty success"
                  ? []
                  : [
                      {
                        block_type: "goto_url",
                        label: "from_a",
                        url: "https://example.test",
                      },
                    ],
              parameters: [
                {
                  key: "from_a",
                  parameter_type: "workflow",
                  workflow_parameter_type: "string",
                },
              ],
              evidence: { recording: { browser_session_id: "pbs-a" } },
            },
          });
      });
      await waitFor(() =>
        expect(result.current.mutation.isPending).toBe(false),
      );
      expect(result.current.graph.nodes).toEqual(graph.nodes);
      expect(result.current.graph.edges).toEqual(graph.edges);
      expect(
        result.current.graph.edges.every((edge) =>
          [edge.source, edge.target].every((id) =>
            result.current.graph.nodes.some((node) => node.id === id),
          ),
        ),
      ).toBe(true);
      expect(useWorkflowParametersStore.getState().parameters).toBe(parameters);
      expect(useRecordingStore.getState()).toBe(recording);
      expect(useWorkflowHasChangesStore.getState()).toBe(pending);
      expect(useRecordingRefinementEvidenceStore.getState().armed).toBe(
        evidence,
      );
      expect(useRecordedBlocksStore.getState().blocks).toBeNull();
      expect(result.current.location).toBe(location);
      expect(onSuccess).not.toHaveBeenCalled();
      expect(toast).not.toHaveBeenCalled();
      expect(mocks.captureRecordBrowser).not.toHaveBeenCalled();
      unmount();
      client.clear();
    },
  );

  it("applies a live owner's recording with connected edges and settles its committing flag", async () => {
    const onSuccess = vi.fn((result, owner: YamlCommitOwner) => {
      useRecordedBlocksStore.getState().setRecordedBlocks(
        result,
        {
          previous: "start",
          next: "adder",
          connectingEdgeType: "default",
        },
        owner,
      );
      useRecordingStore.getState().setIsRecording(false);
    });
    let resolveRequest!: (value: unknown) => void;
    mocks.post.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRequest = resolve;
        }),
    );
    const { result } = renderHook(
      () => {
        const graph = useWorkflowGraphState(
          [
            { id: "start", type: "start", position: { x: 0, y: 0 }, data: {} },
            {
              id: "adder",
              type: "nodeAdder",
              position: { x: 0, y: 0 },
              data: {},
            },
          ] as AppNode[],
          [{ id: "initial", source: "start", target: "adder" }],
        );
        useApplyRecordedBlocks({
          enabled: true,
          nodes: graph.nodes,
          edges: graph.edges,
          doLayout: (nodes, edges) => {
            graph.setNodes(nodes);
            graph.setEdges(edges);
          },
        });
        return {
          graph,
          mutation: useProcessRecordingMutation({
            browserSessionId: "pbs-1",
            onSuccess,
          }),
        };
      },
      { wrapper },
    );
    act(() => result.current.mutation.mutate({ draftSteps: [draftStep] }));
    await waitFor(() => expect(mocks.post).toHaveBeenCalled());
    expect(useRecordingStore.getState().isCommitting).toBe(true);
    await act(async () =>
      resolveRequest({
        data: {
          recording_id: "br-1",
          blocks: [
            {
              block_type: "goto_url",
              label: "recorded",
              url: "https://example.test",
            },
          ],
          parameters: [
            {
              key: "recorded",
              parameter_type: "workflow",
              workflow_parameter_type: "string",
            },
          ],
        },
      }),
    );
    await waitFor(() => expect(result.current.mutation.isSuccess).toBe(true));
    expect(onSuccess).toHaveBeenCalledWith(
      expect.anything(),
      useWorkflowYamlEditorStore.getState().editorOwner,
    );
    expect(
      result.current.graph.nodes.some((node) => node.data.label === "recorded"),
    ).toBe(true);
    expect(result.current.graph.edges).toHaveLength(2);
    expect(
      result.current.graph.edges.every((edge) =>
        [edge.source, edge.target].every((id) =>
          result.current.graph.nodes.some((node) => node.id === id),
        ),
      ),
    ).toBe(true);
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      expect.objectContaining({ key: "recorded" }),
    ]);
    expect(useRecordingStore.getState()).toMatchObject({
      isCommitting: false,
      isRecording: false,
    });
  });
});
