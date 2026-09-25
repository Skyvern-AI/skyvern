// @vitest-environment jsdom

import { AxiosError } from "axios";
import { useEffect, useState, type ReactNode } from "react";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { toast } from "@/components/ui/use-toast";
import type { AppNode } from "@/routes/workflows/editor/nodes";
import { actionNodeDefaultData } from "@/routes/workflows/editor/nodes/ActionNode/types";
import {
  getWorkflowBlocks,
  getWorkflowErrors,
} from "@/routes/workflows/editor/workflowEditorUtils";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { EditorView } from "@codemirror/view";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { parse as parseYaml } from "yaml";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
  usePendingWorkflowSaveRecovery,
  type WorkflowSaveData,
} from "./WorkflowHasChangesStore";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useRecordingStore } from "./useRecordingStore";
import {
  confirmCodeCacheDeletion,
  useSaveWorkflow,
} from "@/routes/workflows/editor/hooks/useSaveWorkflow";
import { useWorkflowParametersStore } from "./WorkflowParametersStore";
import { useWorkflowTitleStore } from "./WorkflowTitleStore";
import {
  beginSaveTransaction,
  beginCopilotAcceptance,
  beginYamlCommit,
  commitYamlDraft,
  registerEditorOwner,
  unregisterEditorOwner,
  createYamlCommitOwner,
  finishCopilotAcceptance,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
  withCopilotAcceptance,
} from "./WorkflowYamlEditorStore";

import { WorkflowSavePendingNotice } from "@/routes/workflows/editor/WorkflowYamlEditor";

const mocks = vi.hoisted(() => ({
  workflowId: "wpid-1",
  post: vi.fn(),
  delete: vi.fn(),
  getClient: vi.fn(),
  get: vi.fn(),
  put: vi.fn(),
  capture: vi.fn(),
  getNodes: vi.fn<() => AppNode[]>(() => []),
}));

vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@xyflow/react")>()),
  useReactFlow: () => ({ getNodes: mocks.getNodes }),
}));
vi.mock("@/routes/workflows/WorkflowPermanentIdContext", () => ({
  useWorkflowPermanentId: () => mocks.workflowId,
}));
vi.mock("@/routes/workflows/hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => ({ data: undefined }),
}));
vi.mock("@/routes/workflows/studio/useStudioPanes", () => ({
  useStudioPanes: () => ({ openPane: vi.fn() }),
}));
vi.mock("@/util/recordBrowserTelemetry", () => ({
  captureRecordBrowser: vi.fn(),
  markRecordBrowserProcessed: vi.fn(),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: mocks.getClient,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(async () => "test-token"),
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: mocks.capture }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

const saveData = {
  title: "Recorded workflow",
  description: null,
  blocks: [],
  parameters: [],
  workflowDefinitionVersion: 1,
  settings: {
    proxyLocation: "RESIDENTIAL",
    runWith: "agent",
  },
  workflow: {
    workflow_permanent_id: "wpid-1",
    version: 1,
    workflow_definition: { version: 1, blocks: [], parameters: [] },
    status: "published",
  },
} as unknown as WorkflowSaveData;

describe("workflow recording attachment", () => {
  afterEach(() => vi.useRealTimers());
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getNodes.mockReset().mockReturnValue([]);
    sessionStorage.clear();
    mocks.workflowId = "wpid-1";
    useRecordingStore.getState().reset();
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid-1"));
    mocks.getClient.mockResolvedValue({
      get: mocks.get,
      post: mocks.post,
      delete: mocks.delete,
      put: mocks.put,
    });
    mocks.put.mockResolvedValue({
      data: {
        ...saveData.workflow,
        title: saveData.title,
        description: saveData.description,
      },
    });
    useWorkflowHasChangesStore.setState({
      getSaveData: () => saveData,
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("keeps a transport-failed PUT reserved despite canonical advancement and allows another workflow", async () => {
    mocks.put.mockRejectedValueOnce(
      new AxiosError("connection reset", "ERR_NETWORK"),
    );
    const hydrate = vi.fn();
    useWorkflowHasChangesStore.setState({
      hasChanges: true,
      hydrateSavedSettings: hydrate,
    });
    const hook = renderHook(() => useWorkflowSave(), { wrapper });
    await act(async () => {
      await expect(hook.result.current.mutateAsync(undefined)).rejects.toThrow(
        "connection reset",
      );
    });
    render(<WorkflowSavePendingNotice />);
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(
      beginSaveTransaction(useWorkflowYamlEditorStore.getState().editorOwner!),
    ).toBe(false);
    renderHook(
      () =>
        usePendingWorkflowSaveRecovery({ ...saveData.workflow, version: 2 }),
      { wrapper },
    );
    expect(hydrate).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
    ).toMatchObject({ persisting: true });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    registerEditorOwner(createYamlCommitOwner("wpid-other"));
    useWorkflowHasChangesStore.setState({
      getSaveData: () => ({
        ...saveData,
        workflow: { ...saveData.workflow, workflow_permanent_id: "wpid-other" },
      }),
    });
    const other = renderHook(() => useWorkflowSave(), { wrapper });
    await act(async () => {
      await other.result.current.mutateAsync(undefined);
    });
    expect(mocks.put).toHaveBeenCalledTimes(2);
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
    ).toBeDefined();
  });

  it("holds Save, YAML commit, and Copilot apply after reload with an unchanged canonical version and offers Discard and Reload", async () => {
    sessionStorage.setItem(
      "workflow-pending-save:wpid-1",
      JSON.stringify({
        workflowPermanentId: "wpid-1",
        baseVersion: 1,
        timestamp: Date.now(),
      }),
    );
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    const owner = createYamlCommitOwner("wpid-1");
    registerEditorOwner(owner);
    const hydrate = vi.fn();
    const commit = vi.fn(async () => true);
    const apply = vi.fn();
    useWorkflowYamlEditorStore.getState().registerCommit(commit);
    useWorkflowHasChangesStore.setState({ hydrateSavedSettings: hydrate });
    const hook = renderHook(
      () => {
        usePendingWorkflowSaveRecovery(saveData.workflow);
        return useWorkflowSave();
      },
      { wrapper },
    );
    render(<WorkflowSavePendingNotice />);

    expect(beginSaveTransaction(owner)).toBe(false);
    await act(async () => {
      await expect(hook.result.current.mutateAsync(undefined)).rejects.toThrow(
        "A save is in progress",
      );
      expect(await commitYamlDraft(true)).toBe(false);
      expect(beginCopilotAcceptance()).toBeNull();
      expect(withCopilotAcceptance(undefined, apply)).toBe(false);
    });
    expect(mocks.put).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(apply).not.toHaveBeenCalled();
    expect(hydrate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Discard pending save" }),
    ).toBeTruthy();
  });

  it.each([false, true])(
    "hydrates canonical before discarding the restored save hold (stale stores: %s)",
    async (edited) => {
      const marker = JSON.stringify({
        workflowPermanentId: "wpid-1",
        baseVersion: 1,
        timestamp: Date.now(),
      });
      sessionStorage.setItem("workflow-pending-save:wpid-1", marker);
      sessionStorage.setItem(
        "workflow-pending-save:wpid-other",
        "other marker",
      );
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      registerEditorOwner(createYamlCommitOwner("wpid-1"));
      const hydrate = vi.fn();
      const currentSaveData = {
        ...saveData,
        title: edited ? "Edited after reload" : "Loaded canonical title",
        description: edited ? "Edited description" : null,
      };
      const canonical = {
        ...saveData.workflow,
        title: "Loaded canonical title",
        description: "Loaded canonical description",
      };
      mocks.get.mockResolvedValueOnce({ data: canonical });
      useWorkflowHasChangesStore.setState({
        getSaveData: () => ({
          ...currentSaveData,
          workflow: canonical,
          title: useWorkflowTitleStore.getState().title,
          description: useWorkflowTitleStore.getState().description,
        }),
        hasChanges: edited,
        hydrateSavedSettings: hydrate,
      });
      useWorkflowTitleStore.setState({
        title: currentSaveData.title,
        description: currentSaveData.description,
      });
      const hook = renderHook(
        () => {
          usePendingWorkflowSaveRecovery(saveData.workflow);
          return useWorkflowSave();
        },
        { wrapper },
      );
      render(<WorkflowSavePendingNotice />);
      await act(async () =>
        fireEvent.click(
          screen.getByRole("button", { name: "Discard pending save" }),
        ),
      );

      expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBeNull();
      expect(sessionStorage.getItem("workflow-pending-save:wpid-other")).toBe(
        "other marker",
      );
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        pendingSaves: {},
        commitOwner: null,
        persistingOwner: null,
        commitInProgress: false,
        committing: false,
        lockKind: null,
      });
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: canonical.title,
        description: canonical.description,
      });
      expect(hydrate).toHaveBeenCalledExactlyOnceWith(canonical, {
        hydrateGraph: true,
      });
      expect(mocks.put).not.toHaveBeenCalled();
      expect(mocks.post).not.toHaveBeenCalled();
      expect(mocks.delete).toHaveBeenCalledExactlyOnceWith(
        "/browser_recordings/br-1",
      );
      expect(mocks.capture).not.toHaveBeenCalled();
      expect(screen.queryByRole("status")).toBeNull();

      await act(async () => {
        await hook.result.current.mutateAsync(undefined);
      });
      expect(mocks.put).toHaveBeenCalledOnce();
      expect(parseYaml(mocks.put.mock.calls[0]![1])).toMatchObject({
        title: canonical.title,
        description: canonical.description,
      });
    },
  );

  it("keeps the restored save locked when Discard cannot read canonical state, then retries", async () => {
    const marker = JSON.stringify({
      workflowPermanentId: "wpid-1",
      baseVersion: 1,
      timestamp: Date.now(),
    });
    sessionStorage.setItem("workflow-pending-save:wpid-1", marker);
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    const owner = createYamlCommitOwner("wpid-1");
    registerEditorOwner(owner);
    const hydrate = vi.fn();
    useWorkflowHasChangesStore.setState({
      hydrateSavedSettings: hydrate,
      getSaveData: () => ({
        ...saveData,
        workflow: { ...saveData.workflow, title: saveData.title },
      }),
    });
    mocks.get.mockRejectedValueOnce(new Error("Canonical unavailable"));
    render(<WorkflowSavePendingNotice />);

    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: "Discard pending save" }),
      ),
    );
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
    expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBe(marker);
    expect(hydrate).not.toHaveBeenCalled();
    expect(beginSaveTransaction(owner)).toBe(false);
    expect(beginCopilotAcceptance()).toBeNull();
    const canonical = {
      ...saveData.workflow,
      version: 2,
      title: "Landed title",
    };
    mocks.get.mockResolvedValueOnce({ data: canonical });
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: "Discard pending save" }),
      ),
    );
    expect(hydrate).toHaveBeenCalledExactlyOnceWith(canonical, {
      hydrateGraph: true,
    });
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBeNull();
  });

  it("offers only Reload for a live PUT, including after an editor remount", async () => {
    let resolvePut!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    const hook = renderHook(() => useWorkflowSave(), { wrapper });
    render(<WorkflowSavePendingNotice />);
    vi.useFakeTimers();
    let saving!: Promise<unknown>;
    await act(async () => {
      saving = hook.result.current.mutateAsync(undefined);
    });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(mocks.put).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Discard pending save" }),
    ).toBeNull();
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    act(() => {
      unregisterEditorOwner(owner);
      registerEditorOwner(createYamlCommitOwner("wpid-1"));
    });
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Discard pending save" }),
    ).toBeNull();
    expect(
      beginSaveTransaction(useWorkflowYamlEditorStore.getState().editorOwner!),
    ).toBe(false);
    await act(async () => {
      resolvePut({ data: saveData.workflow });
      await saving;
    });
  });

  it.each([false, true])(
    "restores a save marker and hydrates only a newer canonical version (local edits: %s)",
    (edited) => {
      sessionStorage.setItem(
        "workflow-pending-save:wpid-1",
        JSON.stringify({
          workflowPermanentId: "wpid-1",
          baseVersion: 1,
          timestamp: Date.now(),
        }),
      );
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      const owner = createYamlCommitOwner("wpid-1");
      registerEditorOwner(owner);
      const hydrate = vi.fn();
      useWorkflowHasChangesStore.setState({
        hasChanges: false,
        hydrateSavedSettings: hydrate,
      });
      const hook = renderHook(
        ({ workflow }) => usePendingWorkflowSaveRecovery(workflow),
        { wrapper, initialProps: { workflow: saveData.workflow } },
      );
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(hydrate).not.toHaveBeenCalled();
      render(<WorkflowSavePendingNotice />);
      expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
      if (edited) useWorkflowHasChangesStore.setState({ hasChanges: true });
      const canonical = {
        ...saveData.workflow,
        title: "Canonical after reload",
        description: "Canonical description",
        version: 2,
      };
      hook.rerender({ workflow: canonical });
      expect(hydrate).toHaveBeenCalledExactlyOnceWith(canonical, {
        hydrateGraph: true,
      });
      expect(useWorkflowTitleStore.getState().title).toBe(canonical.title);
      expect(
        useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
      ).toBeUndefined();
      expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each([200, 422, 500])(
    "writes the save marker before PUT and removes it on HTTP %s",
    async (status) => {
      let markerDuringPut: unknown;
      mocks.put.mockImplementationOnce(() => {
        markerDuringPut = JSON.parse(
          sessionStorage.getItem("workflow-pending-save:wpid-1")!,
        );
        if (status !== 200)
          return Promise.reject(
            new AxiosError("HTTP error", undefined, undefined, undefined, {
              status,
              data: { detail: "HTTP error" },
            } as NonNullable<AxiosError["response"]>),
          );
        return Promise.resolve({
          data: { ...saveData.workflow, title: saveData.title },
        });
      });
      const hook = renderHook(() => useWorkflowSave(), { wrapper });
      await act(async () => {
        await hook.result.current.mutateAsync(undefined).catch(() => undefined);
      });
      expect(mocks.put).toHaveBeenCalledOnce();
      expect(markerDuringPut).toMatchObject({
        workflowPermanentId: "wpid-1",
        baseVersion: 1,
        timestamp: expect.any(Number),
      });
      expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBeNull();
      expect(
        useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
      ).toBeUndefined();
    },
  );

  it.each(["visual", "YAML"])(
    "sends a %s PUT and retains its hold with a notice when session storage is unavailable",
    async (mode) => {
      const storage = vi
        .spyOn(Storage.prototype, "setItem")
        .mockImplementation(() => {
          throw new DOMException("Storage unavailable", "QuotaExceededError");
        });
      let resolvePut!: (value: unknown) => void;
      mocks.put.mockReset().mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePut = resolve;
          }),
      );
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      const hook = renderHook(() => useWorkflowSave(), { wrapper });
      render(<WorkflowSavePendingNotice />);
      if (mode === "YAML") beginYamlCommit(owner);
      let saving!: Promise<unknown>;
      try {
        act(() => {
          saving = hook.result.current
            .mutateAsync(
              mode === "YAML"
                ? {
                    yamlCommit: {
                      owner,
                      revision: useWorkflowYamlEditorStore.getState().revision,
                    },
                  }
                : undefined,
            )
            .catch((error) => error);
        });
        await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
        expect(
          useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
        ).toMatchObject({ persisting: true });
        expect(beginSaveTransaction(owner)).toBe(false);
        expect(beginCopilotAcceptance()).toBeNull();
        expect(screen.getByRole("status").textContent).toContain(
          "Reload protection for this save is unavailable because session storage is unavailable.",
        );
        const returning = createYamlCommitOwner("wpid-1");
        act(() => {
          unregisterEditorOwner(owner);
          registerEditorOwner(returning);
        });
        expect(beginSaveTransaction(returning)).toBe(false);
        expect(screen.getByRole("status").textContent).toContain(
          "Reload protection",
        );
        await act(async () => {
          resolvePut({
            data: {
              ...saveData.workflow,
              title: saveData.title,
              description: saveData.description,
            },
          });
          await saving;
        });
        expect(screen.queryByRole("status")).toBeNull();
        expect(
          useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
        ).toBeUndefined();
      } finally {
        storage.mockRestore();
      }
    },
  );

  it("writes the pending-save marker without a degraded-mode notice when storage works", async () => {
    let resolvePut!: (value: unknown) => void;
    mocks.put.mockReset().mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    const hook = renderHook(() => useWorkflowSave(), { wrapper });
    render(<WorkflowSavePendingNotice />);
    let saving!: Promise<unknown>;
    act(() => {
      saving = hook.result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
    expect(
      JSON.parse(sessionStorage.getItem("workflow-pending-save:wpid-1")!),
    ).toMatchObject({
      workflowPermanentId: "wpid-1",
      baseVersion: 1,
      timestamp: expect.any(Number),
    });
    expect(
      beginSaveTransaction(useWorkflowYamlEditorStore.getState().editorOwner!),
    ).toBe(false);
    expect(screen.queryByText(/Reload protection/)).toBeNull();
    await act(async () => {
      resolvePut({
        data: {
          ...saveData.workflow,
          title: saveData.title,
          description: saveData.description,
        },
      });
      await saving;
    });
    expect(sessionStorage.getItem("workflow-pending-save:wpid-1")).toBeNull();
  });

  it.each([false, true])(
    "A46 hydrates another workflow while a disposed YAML PUT is pending (return: %s)",
    async (returnToOriginal) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      let resolvePut!: (value: unknown) => void;
      mocks.put.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePut = resolve;
          }),
      );
      const hook = renderHook(() => useWorkflowSave(), { wrapper });
      beginYamlCommit(owner);
      let saving!: Promise<unknown>;
      act(() => {
        saving = hook.result.current.mutateAsync({
          yamlCommit: {
            owner,
            revision: useWorkflowYamlEditorStore.getState().revision,
          },
        });
      });
      await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
      hook.unmount();
      unregisterEditorOwner(owner);
      const next = createYamlCommitOwner("wpid-next");
      const parameters = [
        {
          key: "next_input",
          parameterType: "context" as const,
          sourceParameterKey: "source",
        },
      ];
      useWorkflowParametersStore
        .getState()
        .setParameters(parameters, { workflowPermanentId: "wpid-next" });
      useWorkflowTitleStore
        .getState()
        .initializeTitle("Next title", "wpid-next");
      useWorkflowTitleStore
        .getState()
        .initializeDescription("wpid-next", "Next description");
      registerEditorOwner(next);
      expect(useWorkflowParametersStore.getState().parameters).toEqual(
        parameters,
      );
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: "Next title",
        description: "Next description",
      });
      const hydrate = vi.fn();
      useWorkflowHasChangesStore.setState({ hydrateSavedSettings: hydrate });
      if (returnToOriginal) {
        unregisterEditorOwner(next);
        registerEditorOwner(createYamlCommitOwner("wpid-1"));
        expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
          true,
        );
      }
      await act(async () => {
        resolvePut({
          data: {
            ...saveData.workflow,
            title: "Original saved",
            description: "Original description",
          },
        });
        await saving;
      });
      expect(hydrate).toHaveBeenCalledTimes(returnToOriginal ? 1 : 0);
      expect(useWorkflowTitleStore.getState().title).toBe(
        returnToOriginal ? "Original saved" : "Next title",
      );
    },
  );

  it("A46 clears an attached recording after disposed-owner success without deleting it", async () => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    let resolvePut!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    const hook = renderHook(() => useWorkflowSave(), { wrapper });
    let saving!: Promise<unknown>;
    act(() => {
      saving = hook.result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
    hook.unmount();
    unregisterEditorOwner(owner);
    registerEditorOwner(createYamlCommitOwner("wpid-next"));
    await act(async () => {
      resolvePut({ data: saveData.workflow });
      await saving;
    });
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBeNull();
    expect(mocks.delete).not.toHaveBeenCalled();
    mocks.workflowId = "wpid-next";
    mocks.post.mockResolvedValueOnce({
      data: {
        recording_id: "br-next",
        blocks: [{ block_type: "action", label: "next" }],
        parameters: [],
      },
    });
    const process = renderHook(
      () => useProcessRecordingMutation({ browserSessionId: "pbs-next" }),
      { wrapper },
    );
    await act(async () => {
      await process.result.current.mutateAsync({
        draftSteps: [
          {
            step_id: "step-next",
            action_kind: "click",
            block_type: "action",
            label: "Next",
            status: "ready",
            editable_fields: [],
            parameters: [],
            parameter_keys: [],
          },
        ],
      });
    });
    expect(mocks.post).toHaveBeenCalledWith(
      "/browser_sessions/pbs-next/process_recording",
      expect.objectContaining({ workflow_permanent_id: "wpid-next" }),
    );
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
      "br-next",
    );
  });

  it("A47 keeps a slow PUT reserved until its own response despite newer canonical versions", async () => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    let resolvePut!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    let canonical = { ...saveData.workflow, version: 1 };
    const get = vi.fn(async () => ({ data: canonical }));
    mocks.getClient.mockResolvedValue({
      put: mocks.put,
      get,
      delete: mocks.delete,
    });
    useWorkflowHasChangesStore.setState({
      getSaveData: () => ({
        ...saveData,
        workflow: { ...saveData.workflow, version: 1 },
      }),
    });
    const hook = renderHook(() => useWorkflowSave(), { wrapper });
    vi.useFakeTimers();
    beginYamlCommit(owner);
    let saving!: Promise<unknown>;
    act(() => {
      saving = hook.result.current.mutateAsync({
        yamlCommit: {
          owner,
          revision: useWorkflowYamlEditorStore.getState().revision,
        },
      });
    });
    await act(async () => {});
    expect(mocks.put).toHaveBeenCalledOnce();
    let settled = false;
    void saving.then(() => {
      settled = true;
    });
    hook.unmount();
    unregisterEditorOwner(owner);
    const next = createYamlCommitOwner("wpid-next");
    registerEditorOwner(next);
    expect(beginYamlCommit(next)).toBe(true);
    finishYamlCommit(next);
    const nextData = {
      ...saveData,
      title: "Next saved",
      workflow: { ...saveData.workflow, workflow_permanent_id: "wpid-next" },
    };
    useWorkflowHasChangesStore.setState({ getSaveData: () => nextData });
    const nextSave = renderHook(() => useWorkflowSave(), { wrapper });
    mocks.put.mockResolvedValueOnce({
      data: {
        ...nextData.workflow,
        title: nextData.title,
        description: nextData.description,
      },
    });
    await act(async () => {
      await nextSave.result.current.mutateAsync(undefined);
    });
    expect(mocks.put.mock.calls[1]?.[0]).toBe("/workflows/wpid-next");
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"]?.slow,
    ).toBe(true);
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["wpid-next"],
    ).toBeUndefined();
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    unregisterEditorOwner(next);
    registerEditorOwner(createYamlCommitOwner("wpid-1"));
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
    for (const workflow_definition of [
      { version: 1, blocks: [], parameters: [] },
      {
        ...canonical.workflow_definition,
        blocks: [{ block_type: "task", label: "other_writer" }],
      },
    ]) {
      canonical = {
        ...canonical,
        version: canonical.version + 1,
        workflow_definition,
      } as typeof canonical;
      await act(async () => {
        await get();
        await vi.advanceTimersByTimeAsync(2_000);
      });
      expect(settled).toBe(false);
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
      expect(
        beginSaveTransaction(
          useWorkflowYamlEditorStore.getState().editorOwner!,
        ),
      ).toBe(false);
    }
    expect(get).toHaveBeenCalledTimes(2);
    expect(mocks.put.mock.calls[0]?.[2]).not.toHaveProperty("timeout");
    expect(mocks.put.mock.calls[0]?.[2]).not.toHaveProperty("signal");
    const ownResponse = { data: { ...saveData.workflow, version: 4 } };
    await act(async () => {
      resolvePut(ownResponse);
      expect(await saving).toBe(ownResponse);
    });
    expect(settled).toBe(true);
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
    ).toBeUndefined();
    vi.useRealTimers();
  });

  it.each(["conversion failure", "refused", "stale", "disposed"])(
    "clears confirmation approval when YAML Save exits before mutation: %s",
    async (outcome) => {
      const state = useWorkflowYamlEditorStore.getState();
      state.open("blocks: []");
      state.setDraft("blocks: []\ntitle: changed");
      const convert = vi.fn().mockRejectedValue(new Error("conversion failed"));
      state.registerCommit(async () => {
        if (outcome === "conversion failure") {
          try {
            await convert();
          } catch {
            return false;
          }
        }
        if (outcome === "disposed")
          unregisterEditorOwner(
            useWorkflowYamlEditorStore.getState().editorOwner!,
          );
        return false;
      });
      if (outcome === "refused")
        useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
      useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
      const first = renderHook(() => useSaveWorkflow(), { wrapper });
      await act(async () => {
        await expect(first.result.current()).rejects.toThrow();
      });
      expect(mocks.put).not.toHaveBeenCalled();
      expect(
        useWorkflowHasChangesStore.getState().saidOkToCodeCacheDeletion,
      ).toBe(false);
      if (outcome === "conversion failure")
        expect(convert).toHaveBeenCalledOnce();
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      registerEditorOwner(createYamlCommitOwner("wpid-1"));
      const next = renderHook(() => useWorkflowSave(), { wrapper });
      await act(async () => next.result.current.mutateAsync(undefined));
      expect(mocks.put.mock.calls[0]?.[2].params.delete_code_cache_is_ok).toBe(
        "false",
      );
    },
  );

  it("keeps the approved YAML Save when Yes is clicked twice during conversion", async () => {
    let finishConversion!: () => void;
    const conversion = new Promise<void>((resolve) => {
      finishConversion = resolve;
    });
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    const hook = renderHook(
      () => ({ save: useSaveWorkflow(), mutation: useWorkflowSave() }),
      { wrapper },
    );
    const state = useWorkflowYamlEditorStore.getState();
    state.open("blocks: []");
    state.registerCommit(async (_persist, codeCacheDeletionApproved) => {
      if (!beginYamlCommit(owner)) return false;
      await conversion;
      await hook.result.current.mutation.mutateAsync({
        codeCacheDeletionApproved,
        yamlCommit: {
          owner,
          revision: useWorkflowYamlEditorStore.getState().revision,
        },
      });
      finishYamlCommit(owner);
      return true;
    });
    useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
    let first!: Promise<void>;
    act(() => {
      first = hook.result.current.save();
    });
    useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
    await act(async () => {
      await expect(hook.result.current.save()).rejects.toThrow();
    });
    await act(async () => {
      finishConversion();
      await first;
    });
    expect(mocks.put.mock.calls[0]?.[2].params.delete_code_cache_is_ok).toBe(
      "true",
    );
  });

  it.each(["save", "confirmation"] as const)(
    "does not clear a newer approval when disposed conversion settles through %s",
    async (entry) => {
      let finishConversion!: () => void;
      const conversion = new Promise<void>((resolve) => {
        finishConversion = resolve;
      });
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      const hook = renderHook(() => useSaveWorkflow(), { wrapper });
      useWorkflowYamlEditorStore.getState().open("blocks: []");
      useWorkflowYamlEditorStore.getState().registerCommit(async () => {
        await conversion;
        return false;
      });
      useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
      let first!: Promise<void | boolean>;
      act(() => {
        first =
          entry === "confirmation"
            ? confirmCodeCacheDeletion(hook.result.current)
            : hook.result.current();
        void first.catch(() => undefined);
      });
      unregisterEditorOwner(owner);
      registerEditorOwner(createYamlCommitOwner("wpid-next"));
      useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
      await act(async () => {
        finishConversion();
        if (entry === "confirmation") expect(await first).toBe(false);
        else await expect(first).rejects.toThrow();
      });
      expect(
        useWorkflowHasChangesStore.getState().saidOkToCodeCacheDeletion,
      ).toBe(true);
    },
  );

  it.each(["ordinary", "YAML"])(
    "hydrates a revisited workflow under the pending %s lock before marking it clean",
    async (mode) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      let resolvePut!: (value: unknown) => void;
      mocks.put.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePut = resolve;
          }),
      );
      const first = renderHook(() => useWorkflowSave(), { wrapper });
      let saving!: Promise<unknown>;
      act(() => {
        if (mode === "YAML") beginYamlCommit(owner);
        saving = first.result.current.mutateAsync(
          mode === "YAML"
            ? {
                yamlCommit: {
                  owner,
                  revision: useWorkflowYamlEditorStore.getState().revision,
                },
              }
            : undefined,
        );
      });
      await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
      first.unmount();
      unregisterEditorOwner(owner);
      registerEditorOwner(createYamlCommitOwner("wpid-1"));
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
      const saved = {
        ...saveData.workflow,
        title: "Saved on server",
        workflow_definition: {
          version: 1,
          blocks: [{ block_type: "task", label: "saved" }],
          parameters: [],
        },
      };
      let hydrated = false;
      const hydrateSavedSettings = vi.fn(() => {
        expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
          true,
        );
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
        hydrated = true;
      });
      useWorkflowHasChangesStore.setState({
        hasChanges: true,
        hydrateSavedSettings,
      });
      const unsubscribe = useWorkflowHasChangesStore.subscribe((state) => {
        if (!state.hasChanges) expect(hydrated).toBe(true);
      });
      await act(async () => {
        resolvePut({ data: saved });
        await saving;
      });
      unsubscribe();
      expect(hydrateSavedSettings).toHaveBeenCalledExactlyOnceWith(saved, {
        hydrateGraph: true,
      });
      expect(useWorkflowTitleStore.getState().title).toBe("Saved on server");
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
        false,
      );
    },
  );

  it.each(
    ["ordinary", "YAML"].flatMap((mode) =>
      ["success", "error"].map((outcome) => [mode, outcome]),
    ),
  )(
    "A42 consumes cache deletion approval for a disposed %s save on %s",
    async (mode, outcome) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      useWorkflowHasChangesStore.getState().setSaidOkToCodeCacheDeletion(true);
      let resolvePut!: (value: unknown) => void;
      let rejectPut!: (error: Error) => void;
      mocks.put.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolvePut = resolve;
            rejectPut = reject;
          }),
      );
      const first = renderHook(() => useWorkflowSave(), { wrapper });
      if (mode === "YAML") expect(beginYamlCommit(owner)).toBe(true);
      let saving!: Promise<unknown>;
      act(() => {
        saving = first.result.current
          .mutateAsync(
            mode === "YAML"
              ? {
                  yamlCommit: {
                    owner,
                    revision: useWorkflowYamlEditorStore.getState().revision,
                  },
                }
              : undefined,
          )
          .catch((error: unknown) => error);
      });
      await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
      expect(mocks.put.mock.calls[0]?.[2].params.delete_code_cache_is_ok).toBe(
        "true",
      );
      first.unmount();
      unregisterEditorOwner(owner);
      await act(async () => {
        if (outcome === "success") resolvePut({ data: saveData.workflow });
        else rejectPut(new Error("save failed"));
        await saving;
      });
      expect(
        useWorkflowHasChangesStore.getState().saidOkToCodeCacheDeletion,
      ).toBe(false);
      registerEditorOwner(createYamlCommitOwner("wpid-next"));
      useWorkflowHasChangesStore.setState({
        getSaveData: () => ({
          ...saveData,
          workflow: {
            ...saveData.workflow,
            workflow_permanent_id: "wpid-next",
          },
        }),
      });
      const next = renderHook(() => useWorkflowSave(), { wrapper });
      await act(async () => next.result.current.mutateAsync(undefined));
      expect(mocks.put.mock.calls[1]?.[2].params.delete_code_cache_is_ok).toBe(
        "false",
      );
    },
  );

  it.each(["success", "error", "abort"])(
    "reserves an in-flight Save until %s settlement",
    async (outcome) => {
      let resolvePut!: (value: unknown) => void;
      let rejectPut!: (error: Error) => void;
      mocks.put.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolvePut = resolve;
            rejectPut = reject;
          }),
      );
      const failure =
        outcome === "abort"
          ? new AxiosError("aborted", "ERR_CANCELED")
          : new AxiosError(outcome, undefined, undefined, undefined, {
              status: 500,
              data: { detail: "Server error" },
            } as NonNullable<AxiosError["response"]>);
      const commit = vi.fn().mockResolvedValue(true);
      useWorkflowYamlEditorStore.getState().registerCommit(commit);
      const { result } = renderHook(() => useWorkflowSave(), { wrapper });
      let saving!: Promise<unknown>;
      act(() => {
        saving = result.current.mutateAsync(undefined);
      });
      const settled = saving.then(
        () => null,
        (error: Error) => error,
      );
      await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
      expect(beginCopilotAcceptance()).toBeNull();
      expect(beginYamlCommit(createYamlCommitOwner("wpid-1"))).toBe(false);
      expect(await commitYamlDraft(true)).toBe(false);
      expect(commit).not.toHaveBeenCalled();
      await expect(
        act(async () => result.current.mutateAsync(undefined)),
      ).rejects.toThrow("A save is in progress");
      await act(async () => {
        if (outcome === "success")
          resolvePut({
            data: {
              ...saveData.workflow,
              title: saveData.title,
              description: null,
            },
          });
        else rejectPut(failure);
        expect(await settled).toEqual(outcome === "success" ? null : failure);
      });
      expect(mocks.delete).not.toHaveBeenCalled();
      const token = beginCopilotAcceptance();
      if (outcome === "abort") {
        expect(token).toBeNull();
        expect(
          useWorkflowYamlEditorStore.getState().pendingSaves["wpid-1"],
        ).toMatchObject({ persisting: true, slow: true });
      } else {
        expect(token).not.toBeNull();
        finishCopilotAcceptance(token!);
      }
    },
  );

  it("keeps the Save reservation after editor disposal until the PUT settles", async () => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    let resolvePut!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve;
        }),
    );
    const { result, unmount } = renderHook(() => useWorkflowSave(), {
      wrapper,
    });
    let saving!: Promise<unknown>;
    act(() => {
      saving = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
    unmount();
    unregisterEditorOwner(owner);
    useWorkflowYamlEditorStore.getState().setCommitInProgress(false);
    expect(beginCopilotAcceptance()).toBeNull();
    await act(async () => {
      resolvePut({
        data: {
          ...saveData.workflow,
          title: saveData.title,
          description: null,
        },
      });
      await saving;
    });
    const token = beginCopilotAcceptance();
    expect(token).not.toBeNull();
    finishCopilotAcceptance(token!);
  });

  it.each(
    ["ordinary", "YAML commit"].flatMap((mode) =>
      ["success", "network error", "confirmation error"].map((outcome) => [
        mode,
        outcome,
      ]),
    ),
  )(
    "invalidates the submitted workflow after disposal for %s on %s without changing editor state",
    async (mode, outcome) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      const queryClient = new QueryClient({
        defaultOptions: { queries: { staleTime: 5 * 60 * 1000, retry: false } },
      });
      const queryKey = ["workflow", "wpid-1"];
      const otherQueryKey = ["workflow", "wpid-other"];
      const originalWorkflow = { ...saveData.workflow, title: "Before save" };
      const serverWorkflow = {
        ...originalWorkflow,
        title: outcome === "success" ? "Saved title" : originalWorkflow.title,
      };
      queryClient.setQueryData(queryKey, originalWorkflow);
      queryClient.setQueryData(["workflows"], []);
      queryClient.setQueryData(["block-scripts", "wpid-1"], []);
      queryClient.setQueryData(otherQueryKey, { title: "Other workflow" });
      function cacheWrapper({ children }: { children: ReactNode }) {
        return (
          <QueryClientProvider client={queryClient}>
            {children}
          </QueryClientProvider>
        );
      }
      let resolvePut!: (value: unknown) => void;
      let rejectPut!: (error: Error) => void;
      mocks.put.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            resolvePut = resolve;
            rejectPut = reject;
          }),
      );
      const failure = new AxiosError("save failed");
      if (outcome === "confirmation error") {
        failure.response = {
          data: { detail: "No confirmation for code cache deletion" },
        } as NonNullable<AxiosError["response"]>;
      }
      let currentSaveData = saveData;
      useWorkflowHasChangesStore.setState({
        getSaveData: () => currentSaveData,
      });
      const { result, unmount } = renderHook(() => useWorkflowSave(), {
        wrapper: cacheWrapper,
      });
      if (mode === "YAML commit") expect(beginYamlCommit(owner)).toBe(true);
      let saving!: Promise<unknown>;
      act(() => {
        saving = result.current.mutateAsync(
          mode === "YAML commit"
            ? {
                yamlCommit: {
                  owner,
                  revision: useWorkflowYamlEditorStore.getState().revision,
                },
              }
            : undefined,
        );
      });
      const settled = saving.catch((error: unknown) => error);
      await waitFor(() => expect(mocks.put).toHaveBeenCalledOnce());
      expect(mocks.put.mock.calls[0]?.[0]).toBe("/workflows/wpid-1");
      unmount();
      unregisterEditorOwner(owner);
      registerEditorOwner(createYamlCommitOwner("wpid-other"));
      currentSaveData = {
        ...saveData,
        workflow: { ...saveData.workflow, workflow_permanent_id: "wpid-other" },
      };
      const hydrateSavedSettings = vi.fn();
      useWorkflowHasChangesStore.setState({
        hydrateSavedSettings,
        hasChanges: true,
        saidOkToCodeCacheDeletion: true,
        showConfirmCodeCacheDeletion: false,
      });
      const editorState = useWorkflowHasChangesStore.getState();
      const metadataState = useWorkflowTitleStore.getState();
      expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(false);
      await act(async () => {
        if (outcome === "success") resolvePut({ data: serverWorkflow });
        else rejectPut(failure);
        const result = await settled;
        if (outcome !== "success") expect(result).toBe(failure);
      });
      expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true);
      expect(queryClient.getQueryState(["workflows"])?.isInvalidated).toBe(
        true,
      );
      expect(
        queryClient.getQueryState(["block-scripts", "wpid-1"])?.isInvalidated,
      ).toBe(true);
      expect(queryClient.getQueryState(otherQueryKey)?.isInvalidated).toBe(
        false,
      );
      expect(hydrateSavedSettings).not.toHaveBeenCalled();
      expect(useWorkflowHasChangesStore.getState()).toEqual(
        outcome === "success"
          ? {
              ...editorState,
              pendingRecordingId: null,
              pendingRecordingWorkflowPermanentId: null,
            }
          : editorState,
      );
      expect(useWorkflowTitleStore.getState()).toBe(metadataState);
      const refetch = vi.fn().mockResolvedValue(serverWorkflow);
      const reopened = renderHook(
        () => useQuery({ queryKey, queryFn: refetch }),
        { wrapper: cacheWrapper },
      );
      await waitFor(() => expect(refetch).toHaveBeenCalledOnce());
      await waitFor(() =>
        expect(reopened.result.current.data).toEqual(serverWorkflow),
      );
      reopened.unmount();
      queryClient.clear();
    },
  );

  it("sends the pending recording on save and clears it only after success", async () => {
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => result.current.mutateAsync(undefined));

    const call = mocks.put.mock.calls[0];
    expect(call).toBeDefined();
    const yamlBody = call?.[1] as string;
    expect(parseYaml(yamlBody)).toMatchObject({ recording_id: "br-1" });
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().pendingRecordingId,
      ).toBeNull(),
    );
    expect(mocks.delete).not.toHaveBeenCalled();
  });

  it.each(["before save", "while credentials load"])(
    "preserves the draft when authoring starts %s",
    async (timing) => {
      useWorkflowHasChangesStore.setState({ hasChanges: true });
      if (timing === "before save") {
        useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
      } else {
        mocks.getClient.mockImplementationOnce(async () => {
          useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
          return { delete: mocks.delete, put: mocks.put };
        });
      }
      const { result } = renderHook(() => useWorkflowSave(), { wrapper });

      await expect(
        act(async () => result.current.mutateAsync(undefined)),
      ).rejects.toThrow("Finish the current authoring action before saving");
      expect(mocks.put).not.toHaveBeenCalled();
      expect(mocks.delete).not.toHaveBeenCalled();
      expect(useWorkflowHasChangesStore.getState()).toMatchObject({
        hasChanges: true,
        pendingRecordingId: "br-1",
      });

      useWorkflowYamlEditorStore.setState({ authoringInProgress: false });
      await act(async () => result.current.mutateAsync(undefined));
      expect(parseYaml(mocks.put.mock.calls[0]?.[1])).toMatchObject({
        recording_id: "br-1",
      });
    },
  );

  it("refuses Save when Copilot already reserves the workflow", async () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    const reservation = beginCopilotAcceptance()!;
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("Wait for the Copilot change to finish");
    expect(mocks.getClient).not.toHaveBeenCalled();
    expect(mocks.put).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: true,
      pendingRecordingId: "br-1",
    });
    finishCopilotAcceptance(reservation);
  });

  it("reserves Save before credentials load and refuses a competing Save", async () => {
    let resolveClient!: (value: unknown) => void;
    mocks.getClient.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveClient = resolve;
        }),
    );
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    let saving!: Promise<unknown>;
    act(() => {
      saving = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(mocks.getClient).toHaveBeenCalledOnce());
    expect(beginCopilotAcceptance()).toBeNull();
    expect(beginYamlCommit(createYamlCommitOwner("wpid-1"))).toBe(false);
    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("A save is in progress");
    expect(beginCopilotAcceptance()).toBeNull();
    await act(async () => {
      resolveClient({ put: mocks.put, delete: mocks.delete });
      await saving;
    });
    expect(mocks.put).toHaveBeenCalledOnce();
    const token = beginCopilotAcceptance();
    expect(token).not.toBeNull();
    finishCopilotAcceptance(token!);
  });

  it("allows only the matching YAML transaction to save", async () => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    expect(beginYamlCommit(owner)).toBe(true);
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("A YAML commit is in progress");
    expect(mocks.put).not.toHaveBeenCalled();
    await act(async () =>
      result.current.mutateAsync({
        yamlCommit: {
          owner,
          revision: useWorkflowYamlEditorStore.getState().revision,
        },
        title: "Committed YAML title",
      }),
    );
    expect(mocks.put).toHaveBeenCalledOnce();
    expect(parseYaml(mocks.put.mock.calls[0]?.[1])).toMatchObject({
      title: "Committed YAML title",
      recording_id: "br-1",
    });
    expect(useWorkflowYamlEditorStore.getState().commitOwner).toBe(owner);
    await act(async () => {
      useWorkflowHasChangesStore
        .getState()
        .setHasChanges(false, { fromYamlCommit: true });
    });
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBeNull();
    expect(mocks.delete).not.toHaveBeenCalled();
    finishYamlCommit(owner);
  });

  it("releases Save when credential loading fails", async () => {
    mocks.getClient.mockRejectedValueOnce(new Error("credentials unavailable"));
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("credentials unavailable");
    expect(mocks.put).not.toHaveBeenCalled();
    const token = beginCopilotAcceptance();
    expect(token).not.toBeNull();
    finishCopilotAcceptance(token!);
  });

  it("deletes an unattached recording when changes are discarded", async () => {
    renderHook(() => useWorkflowSave(), { wrapper });

    useWorkflowHasChangesStore.getState().setHasChanges(false);

    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: false,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
    });
    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
    expect(mocks.getClient).toHaveBeenCalledWith(
      expect.any(Function),
      "sans-api-v1",
    );
  });

  it("keeps discard deletion registered while another save hook remains mounted", async () => {
    renderHook(() => useWorkflowSave(), { wrapper });
    const latestHook = renderHook(() => useWorkflowSave(), { wrapper });
    latestHook.unmount();

    useWorkflowHasChangesStore.getState().setHasChanges(false);

    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
  });

  it("discards a recording for another workflow after this workflow saves", async () => {
    useWorkflowHasChangesStore.setState({
      pendingRecordingWorkflowPermanentId: "wpid-other",
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => result.current.mutateAsync(undefined));

    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: false,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
    });
  });

  it("does not replace the first recording waiting to be saved", () => {
    useWorkflowHasChangesStore.getState().setPendingRecording("br-2", "wpid-1");

    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("keeps the pending recording when the workflow save fails", async () => {
    mocks.put.mockRejectedValue(new Error("save failed"));
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("save failed");

    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
      "br-1",
    );
  });

  it("keeps the pending recording when header validation fails", async () => {
    useWorkflowHasChangesStore.setState({
      getSaveData: () => ({
        ...saveData,
        settings: { ...saveData.settings, extraHttpHeaders: "{" },
      }),
      hasChanges: true,
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow();

    expect(mocks.put).not.toHaveBeenCalled();
    expect(mocks.delete).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: true,
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("initializes another workflow clean while the previous workflow save stays pending", async () => {
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    const firstOwner = useWorkflowYamlEditorStore.getState().editorOwner!;
    let resolveSave!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    let saving!: Promise<unknown>;
    act(() => {
      saving = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    act(() => {
      unregisterEditorOwner(firstOwner);
      registerEditorOwner(createYamlCommitOwner("wpid-2"));
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    await act(async () => {
      resolveSave({ data: saveData.workflow });
      await saving;
    });
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: false,
      pendingRecordingId: null,
      saveGenerationsByWorkflow: {},
    });
    expect(
      useWorkflowHasChangesStore.getState().saveGenerationsByWorkflow["wpid-2"],
    ).toBeUndefined();
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
  });

  it("clears a revisited workflow's dirty state after its previous owner's save settles", async () => {
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    const firstOwner = useWorkflowYamlEditorStore.getState().editorOwner!;
    const savedWorkflow = {
      ...saveData.workflow,
      title: saveData.title,
      description: saveData.description,
    };
    const hydrateSavedSettings = vi.fn(() => {
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    });
    let resolveSave!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    let saving!: Promise<unknown>;
    act(() => {
      saving = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);

    act(() => {
      unregisterEditorOwner(firstOwner);
      registerEditorOwner(
        createYamlCommitOwner(firstOwner.workflowPermanentId),
      );
      useWorkflowHasChangesStore.setState({ hydrateSavedSettings });
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

    await act(async () => {
      resolveSave({ data: savedWorkflow });
      await saving;
    });
    expect(hydrateSavedSettings).toHaveBeenCalledExactlyOnceWith(
      savedWorkflow,
      {
        hydrateGraph: true,
      },
    );
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(mocks.delete).not.toHaveBeenCalled();
  });

  it.each(["before save", "while credentials load"])(
    "refuses Save when Copilot reserves the workflow %s",
    async (timing) => {
      useWorkflowHasChangesStore.setState({ hasChanges: true });
      let reservation: symbol | null = null;
      if (timing === "before save") {
        reservation = beginCopilotAcceptance();
      } else {
        mocks.getClient.mockImplementationOnce(async () => {
          expect(beginCopilotAcceptance()).toBeNull();
          // Revalidate even if a writer bypasses the transaction entry point.
          reservation = Symbol("competing Copilot reservation");
          useWorkflowYamlEditorStore.setState({
            copilotAcceptance: reservation,
            lockKind: "copilot",
          });
          return { delete: mocks.delete, put: mocks.put };
        });
      }
      const { result } = renderHook(() => useWorkflowSave(), { wrapper });

      await expect(
        act(async () => result.current.mutateAsync(undefined)),
      ).rejects.toThrow("Wait for the Copilot change to finish");
      expect(mocks.getClient).toHaveBeenCalledTimes(
        timing === "before save" ? 0 : 1,
      );
      expect(mocks.put).not.toHaveBeenCalled();
      expect(mocks.delete).not.toHaveBeenCalled();
      expect(useWorkflowHasChangesStore.getState()).toMatchObject({
        hasChanges: true,
        pendingRecordingId: "br-1",
      });
      expect(useWorkflowHasChangesStore.getState().getSaveData()).toBe(
        saveData,
      );
      finishCopilotAcceptance(reservation!);
    },
  );

  it.each(["before save", "while credentials load"])(
    "allows only the matching YAML transaction when a commit starts %s",
    async (timing) => {
      const owner = createYamlCommitOwner("wpid-1");
      useWorkflowHasChangesStore.setState({ hasChanges: true });
      if (timing === "before save") {
        expect(beginYamlCommit(owner)).toBe(true);
      } else {
        mocks.getClient.mockImplementationOnce(async () => {
          expect(beginYamlCommit(owner)).toBe(false);
          // Revalidate ownership even if a writer bypasses the transaction entry point.
          useWorkflowYamlEditorStore.setState({
            commitOwner: owner,
            commitInProgress: true,
            lockKind: "yaml",
          });
          return { delete: mocks.delete, put: mocks.put };
        });
      }
      const { result } = renderHook(() => useWorkflowSave(), { wrapper });
      await expect(
        act(async () => result.current.mutateAsync(undefined)),
      ).rejects.toThrow("A YAML commit is in progress");
      expect(mocks.getClient).toHaveBeenCalledTimes(
        timing === "before save" ? 0 : 1,
      );
      expect(mocks.put).not.toHaveBeenCalled();
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

      await act(async () =>
        result.current.mutateAsync({
          yamlCommit: {
            owner,
            revision: useWorkflowYamlEditorStore.getState().revision,
          },
          title: "Committed YAML title",
        }),
      );
      expect(mocks.put).toHaveBeenCalledTimes(1);
      expect(parseYaml(mocks.put.mock.calls[0]?.[1])).toMatchObject({
        title: "Committed YAML title",
        recording_id: "br-1",
      });
      finishYamlCommit(owner);
    },
  );

  it("does not hydrate over authoring that starts while a save is pending", async () => {
    const hydrate = vi.fn();
    useWorkflowHasChangesStore.setState({
      hasChanges: true,
      hydrateSavedSettings: hydrate,
    });
    let resolveSave!: (value: unknown) => void;
    mocks.put.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });
    let saving!: Promise<unknown>;
    act(() => {
      saving = result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    const rejected = expect(saving).rejects.toThrow(
      "Saved on the server, but local edits changed during the save; reload.",
    );
    await act(async () => {
      useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
      resolveSave({ data: saveData.workflow });
      await rejected;
    });
    expect(hydrate).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: true,
      pendingRecordingId: null,
      saveGenerationsByWorkflow: { "wpid-1": 1 },
    });
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
  });

  it("saves a corrected required Action Instruction before the debounce without a destructive toast", async () => {
    let saving!: Promise<void>;
    function BufferedAction() {
      const [instruction, setInstruction] = useState("");
      const save = useSaveWorkflow();
      useEffect(() => {
        const nodes: AppNode[] = [
          {
            id: "action",
            type: "action",
            position: { x: 0, y: 0 },
            data: {
              ...actionNodeDefaultData,
              label: "action",
              navigationGoal: instruction,
            },
          },
        ];
        mocks.getNodes.mockImplementation(() => nodes);
        useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
          ...saveData,
          blocks: getWorkflowBlocks(nodes, []),
        }));
      }, [instruction]);
      return (
        <>
          <WorkflowBlockInputTextarea
            nodeId="action"
            name="navigationGoal"
            aria-label="Action Instruction"
            value={instruction}
            onChange={setInstruction}
            hideActions
          />
          <button
            onClick={() => {
              saving = save();
            }}
          >
            Save
          </button>
        </>
      );
    }
    const client = new QueryClient();
    const view = render(
      <QueryClientProvider client={client}>
        <ReactFlowProvider>
          <BufferedAction />
        </ReactFlowProvider>
      </QueryClientProvider>,
    );
    vi.useFakeTimers();
    try {
      const instruction = "Click the Continue button";
      fireEvent.change(
        screen.getByRole("textbox", { name: "Action Instruction" }),
        {
          target: { value: instruction },
        },
      );
      expect(getWorkflowErrors(mocks.getNodes())).toEqual([
        "action: Action Instruction is required.",
      ]);
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()?.blocks[0],
      ).toMatchObject({
        navigation_goal: "",
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save" }));
        await saving;
      });
      expect(mocks.put).toHaveBeenCalledOnce();
      expect(
        parseYaml(mocks.put.mock.calls[0]![1]).workflow_definition.blocks[0],
      ).toMatchObject({
        navigation_goal: instruction,
      });
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
    } finally {
      view.unmount();
      client.clear();
      vi.useRealTimers();
    }
  });

  it.each(["textarea", "code"] as const)(
    "includes buffered %s edits in a programmatic save before the debounce",
    async (kind) => {
      vi.stubGlobal("IntersectionObserver", undefined);
      let save!: () => Promise<unknown>;
      function BufferedBlock() {
        const [text, setText] = useState("original");
        const mutation = useWorkflowSave();
        save = () => mutation.mutateAsync(undefined);
        useEffect(() => {
          useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
            ...saveData,
            blocks: [
              {
                block_type: "code",
                label: "code",
                code: text,
                error_code_mapping: null,
              },
            ],
          }));
        }, [text]);
        return kind === "textarea" ? (
          <WorkflowBlockInputTextarea
            nodeId="code"
            name="code"
            aria-label="Buffered instructions"
            value={text}
            onChange={setText}
            hideActions
          />
        ) : (
          <CodeEditor value={text} onChange={setText} deferKey="code" />
        );
      }
      const client = new QueryClient();
      const view = render(
        <QueryClientProvider client={client}>
          <ReactFlowProvider>
            <BufferedBlock />
          </ReactFlowProvider>
        </QueryClientProvider>,
      );
      vi.useFakeTimers();
      try {
        const newest = "new instructions before autoplay";
        act(() => {
          if (kind === "textarea")
            fireEvent.change(
              screen.getByRole("textbox", { name: "Buffered instructions" }),
              { target: { value: newest } },
            );
          else {
            const editor = EditorView.findFromDOM(
              view.container.querySelector<HTMLElement>(".cm-content")!,
            )!;
            editor.dispatch({
              changes: { from: 0, to: editor.state.doc.length, insert: newest },
            });
          }
        });
        expect(
          useWorkflowHasChangesStore.getState().getSaveData()?.blocks[0],
        ).toMatchObject({ code: "original" });
        await act(async () => {
          await save();
        });
        expect(mocks.put).toHaveBeenCalledOnce();
        expect(
          parseYaml(mocks.put.mock.calls[0]![1]).workflow_definition.blocks[0],
        ).toMatchObject({ code: newest });
        await act(async () => vi.advanceTimersByTimeAsync(300));
        expect(
          useWorkflowHasChangesStore.getState().getSaveData()?.blocks[0],
        ).toMatchObject({ code: newest });
      } finally {
        view.unmount();
        client.clear();
        vi.useRealTimers();
        vi.unstubAllGlobals();
      }
    },
  );
});

describe("blocked saves", () => {
  it("refuses to write the workflow and keeps it dirty while a save is blocked", async () => {
    mocks.put.mockClear();
    mocks.getClient.mockResolvedValue({ delete: mocks.delete, put: mocks.put });
    useWorkflowHasChangesStore.setState({
      getSaveData: () => saveData,
      hasChanges: true,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
      saveBlockedReason: "An Accept's outcome is still unconfirmed.",
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => {
      await expect(result.current.mutateAsync(undefined)).rejects.toBeTruthy();
    });

    expect(mocks.put).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    useWorkflowHasChangesStore.setState({ saveBlockedReason: null });
  });
});
