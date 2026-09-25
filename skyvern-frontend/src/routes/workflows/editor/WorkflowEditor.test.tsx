// @vitest-environment jsdom

import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  createMemoryRouter,
  RouterProvider,
  Link,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AxiosError } from "axios";
import { parse, stringify } from "yaml";

import {
  DebugStoreContext,
  DebugStoreProvider,
} from "@/store/DebugStoreContext";
import { summarizeWorkflowChanges, snapshotOf } from "./workflowChangesSummary";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ProxySelector } from "@/components/ProxySelector";
import { TitleSection } from "../studio/StudioTopBar";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import type { WorkflowCopilotChatHistoryResponse } from "../copilot/workflowCopilotTypes";
import { Status } from "@/api/types";
import { toast } from "@/components/ui/use-toast";
import {
  clearDeferredEdits,
  deferredEdits,
  useDeferredLockedEdit,
} from "@/hooks/useDeferredLockedEdit";

import * as workflowHasChangesModule from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  useWorkflowSave,
  SaveRefusedError,
  SaveStaleError,
  useWorkflowHasChangesStore,
  useHydrateWorkflowParameters,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { applySopResultAtCurrentAppend } from "./workspaceAuthoringActions";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import {
  commitYamlDraft,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  filterWorkflowChanges,
  beginSaveTransaction,
  beginYamlCommit,
  createYamlCommitOwner,
  finishSaveTransaction,
  finishYamlCommit,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type {
  CredentialParameter,
  WorkflowApiResponse,
} from "../types/workflowTypes";
import { WorkflowEditor } from "./WorkflowEditor";
import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import * as xyflowModule from "@xyflow/react";
import * as workflowEditorUtilsModule from "./workflowEditorUtils";
import * as flowRendererModule from "./FlowRenderer";
import { apiWorkflowToSettings } from "./apiWorkflowToSettings";
import {
  buildWorkflowCopilotContext,
  buildWorkflowSaveRequest,
  buildWorkflowYamlDocument,
} from "./workflowYamlDocument";
import {
  getElements,
  getWorkflowSettings,
  getWorkflowBlocks,
} from "./workflowEditorUtils";

import { getInitialParameters } from "./utils";
import { Workspace } from "./Workspace";
import { NodeHeader } from "./nodes/components/NodeHeader";
import {
  applyNodeChanges,
  ReactFlowProvider,
  useStoreApi,
  type NodeChange,
} from "@xyflow/react";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import type { AppNode } from "./nodes";
import { codeBlockNodeDefaultData } from "./nodes/CodeBlockNode/types";
import { BlockActionContext } from "@/store/BlockActionContext";
import * as axiosClientModule from "@/api/AxiosClient";
import * as copilotChatModule from "../copilot/WorkflowCopilotChat";
import * as comparisonPanelModule from "./panels/WorkflowComparisonPanel";
import * as saveWorkflowModule from "./hooks/useSaveWorkflow";
import { EditorOverflowMenu } from "./header/EditorOverflowMenu";

import { WorkflowSettingsEditor } from "./nodes/StartNode/WorkflowSettingsEditor";
import { NavigationEditor } from "./nodes/NavigationNode/NavigationEditor";

import { WorkflowPermanentIdContext } from "../WorkflowPermanentIdContext";
import { WorkflowScopeContext } from "./WorkflowScopeContext";
import { useDeferredTitleEdit } from "../hooks/useDeferredTitleEdit";

import {
  useEffect,
  useLayoutEffect,
  useReducer,
  useState,
  type ReactNode,
} from "react";
import * as workflowHeaderModule from "./WorkflowHeader";

vi.mock("@/components/ui/use-toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/use-toast")>()),
  toast: vi.fn(),
}));

const {
  workflowQueryMock,
  runQueryMock,
  runsQueryMock,
  debugQueryMock,
  activeRunQueryMock,
  put,
  post,
  canvas,
  integration,
  historyGet,
  streams,
  postStreaming,
} = vi.hoisted(() => ({
  integration: {
    chat: false,
    canvas: false,
    studio: false,
    mockSave: false,
    realNavigation: false,
  },
  historyGet: vi.fn(),
  streams: [] as Array<{
    onMessage: (payload: unknown) => boolean;
    resolve: () => void;
  }>,
  postStreaming: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  canvas: { current: null as FlowRendererProps | null },
  workflowQueryMock: vi.fn(),
  runQueryMock: vi.fn(),
  runsQueryMock: vi.fn(),
  debugQueryMock: vi.fn(),
  activeRunQueryMock: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({ put, post, get: historyGet }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("posthog-js/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("posthog-js/react")>()),
  usePostHog: () => ({ capture: vi.fn() }),
}));

const { saveSpy, navBlocker } = vi.hoisted(() => ({
  saveSpy: vi.fn(),
  navBlocker: { state: "unblocked", proceed: vi.fn(), reset: vi.fn() },
}));

vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return { ...actual, useNodesInitialized: vi.fn(actual.useNodesInitialized) };
});

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useBlocker: (...args: Parameters<typeof actual.useBlocker>) =>
      integration.realNavigation ? actual.useBlocker(...args) : navBlocker,
  };
});
vi.mock("@/store/WorkflowHasChangesStore", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/store/WorkflowHasChangesStore")>();
  return {
    ...actual,
    useWorkflowSave: () =>
      integration.mockSave
        ? { mutateAsync: saveSpy, isPending: false }
        : actual.useWorkflowSave(),
  };
});
vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => runQueryMock(),
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => true,
}));
vi.mock("../hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => workflowQueryMock(),
}));
vi.mock("../hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => runQueryMock(),
}));
vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));
vi.mock("../hooks/useBlockScriptsQuery", () => ({
  useBlockScriptsQuery: () => ({ data: undefined }),
}));
vi.mock("../hooks/useCacheKeyValuesQuery", () => ({
  useCacheKeyValuesQuery: () => ({ data: undefined, isLoading: false }),
}));
vi.mock("../hooks/useDebugSessionQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../hooks/useDebugSessionQuery")>()),
  useDebugSessionQuery: () => debugQueryMock(),
}));
vi.mock("../hooks/useActiveRunSessionQuery", () => ({
  useActiveRunSessionQuery: () => activeRunQueryMock(),
}));
vi.mock("@/hooks/useRuntimeConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useRuntimeConfig")>()),
  useStreamTransport: () => ({ streamTransport: "vnc" }),
}));
vi.mock("./FlowRenderer", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./FlowRenderer")>();
  return {
    FlowRenderer: (props: FlowRendererProps) => {
      if (!props.readOnly) canvas.current = props;
      if (integration.canvas) return <actual.FlowRenderer {...props} />;
      return <CanvasStub {...props} />;
    },
  };
  function CanvasStub(props: FlowRendererProps) {
    if (!props.readOnly) canvas.current = props;
    const { onLayoutPhaseChange, initialTitle, workflow, readOnly } = props;
    useEffect(() => {
      if (integration.chat && !readOnly) {
        useWorkflowTitleStore
          .getState()
          .initializeTitle(initialTitle, workflow.workflow_permanent_id);
        useWorkflowTitleStore
          .getState()
          .initializeDescription(
            workflow.workflow_permanent_id,
            workflow.description,
          );
      }
    }, [initialTitle, workflow, readOnly]);
    useEffect(() => {
      onLayoutPhaseChange?.("ready");
    }, [onLayoutPhaseChange]);
    return null;
  }
});
vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: ({ isExecuting }: { isExecuting: boolean }) => (
    <div data-testid="browser-stream" data-executing={isExecuting} />
  ),
}));
vi.mock("@/components/Splitter", () => ({
  Splitter: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("./WorkflowHeader", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./WorkflowHeader")>()),
  WorkflowHeader: () => null,
}));
vi.mock("../copilot/WorkflowCopilotChat", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../copilot/WorkflowCopilotChat")>();
  return {
    ...actual,
    WorkflowCopilotChat: (
      props: Parameters<typeof actual.WorkflowCopilotChat>[0],
    ) =>
      integration.chat ? (
        <actual.WorkflowCopilotChat
          {...props}
          isOpen
          requiresLiveBrowser={false}
        />
      ) : null,
  };
});
vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));
vi.mock("@/hooks/useSpeechToTextField", () => ({
  useSpeechToTextField: () => ({
    isListening: false,
    isSupported: false,
    stop: vi.fn(),
    takeAudioBlob: vi.fn(),
  }),
}));
vi.mock("./recording/RecordingPanel", () => ({
  RecordingPanel: () => <div data-testid="standalone-recording-panel" />,
}));
vi.mock("../debugger/DebuggerRun", () => ({ DebuggerRun: () => null }));
vi.mock("../debugger/DebuggerRunMinimal", () => ({
  DebuggerRunMinimal: () => null,
}));
vi.mock("../debugger/recentActivity/RecentActivityRunSelector", () => ({
  RecentActivityRunSelector: () => null,
}));
// Pane bodies own their data wiring (tested in their own suites); the shell
// contract under test is which of them mount and how the chrome degrades.
vi.mock("../studio/EditorTab", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../studio/EditorTab")>();
  return {
    EditorTab: (props: Parameters<typeof actual.EditorTab>[0]) =>
      integration.studio ? (
        <actual.EditorTab {...props} />
      ) : (
        <div data-testid="editor-tab-body" />
      ),
  };
});
vi.mock("../studio/BrowserTab", () => ({
  BrowserTab: () => <div data-testid="browser-tab-body" />,
}));
vi.mock("../studio/RunTab", () => ({
  RunTab: () => <div data-testid="run-view-body" />,
}));
vi.mock("../studio/StudioBrowserStream", () => ({
  StudioBrowserStream: () => null,
}));
vi.mock("../studio/StudioWorkflowPanels", () => ({
  StudioWorkflowPanels: () => null,
}));
vi.mock("../studio/runview/RunPaneHeader", () => ({
  RunPaneViewToggles: () => null,
  RunPaneActions: () => null,
}));
vi.mock("@/components/onboarding/ProductTour", () => ({
  ProductTour: () => null,
}));
vi.mock("../components/CodeEditor", () => ({ CodeEditor: () => null }));

vi.mock("@uiw/react-codemirror", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@uiw/react-codemirror")>()),
  default: ({
    value,
    onChange,
    onBlur,
    readOnly,
  }: {
    value: string;
    onChange: (value: string) => void;
    onBlur: () => void;
    readOnly: boolean;
  }) => (
    <textarea
      aria-label="Buffered code"
      value={value}
      readOnly={readOnly}
      onChange={(event) => onChange(event.target.value)}
      onBlur={onBlur}
    />
  ),
}));

const deletedWorkflow = {
  workflow_permanent_id: "wpid_del",
  title: "Deleted agent",
  deleted_at: "2026-07-01T12:00:00Z",
  workflow_definition: { blocks: [], parameters: [] },
  proxy_location: null,
  webhook_callback_url: null,
  persist_browser_session: false,
  cache_key: "",
} as never;

const liveWorkflow = {
  workflow_permanent_id: "wpid_live",
  title: "Live agent",
  deleted_at: null,
  workflow_definition: { blocks: [], parameters: [] },
  proxy_location: null,
  webhook_callback_url: null,
  persist_browser_session: false,
  cache_key: "",
} as never;

function makeRun(workflow: unknown) {
  return {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    parameters: {},
    workflow,
  } as never;
}

function renderStudioAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/studio"
            element={<WorkflowEditor />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderSettingsWorkspace(workflow: WorkflowApiResponse) {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const { nodes, edges } = getElements(
    workflow.workflow_definition.blocks,
    apiWorkflowToSettings(workflow),
    true,
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[`/agents/${workflow.workflow_permanent_id}/edit`]}
      >
        <WorkflowPermanentIdContext.Provider
          value={workflow.workflow_permanent_id}
        >
          <ReactFlowProvider>
            <DebugStoreContext.Provider
              value={{ isDebugMode: false, blockRunsEnabled: false }}
            >
              <Workspace
                initialNodes={nodes}
                initialEdges={edges}
                initialTitle={workflow.title}
                workflow={workflow}
                embedded
              />
              <WorkflowSettingsEditor
                blockId={nodes.find((node) => node.type === "start")!.id}
              />
            </DebugStoreContext.Provider>
          </ReactFlowProvider>
        </WorkflowPermanentIdContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

afterEach(cleanup);

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  useRecordingStore.getState().reset();
  vi.mocked(axiosClientModule.getClient).mockResolvedValue({
    put,
    post,
    get: historyGet,
  } as never);
  integration.chat = false;
  integration.studio = false;
  integration.mockSave = false;
  integration.realNavigation = false;
  saveSpy.mockReset();
  integration.canvas = true;
  runQueryMock.mockReturnValue({ data: undefined });
  runsQueryMock.mockReturnValue({ data: [], isPending: false });
  debugQueryMock.mockReturnValue({
    data: {
      debug_session_id: "debug_1",
      browser_session_id: "browser_1",
      vnc_streaming_supported: true,
    },
  });
  activeRunQueryMock.mockReturnValue({
    data: { active_run_session_id: "pbs_run" },
  });
});

describe("WorkflowEditor deleted-agent run fallback", () => {
  test("renders the studio run view from the run snapshot when the workflow query 404s", () => {
    workflowQueryMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    runQueryMock.mockReturnValue({
      data: makeRun(deletedWorkflow),
      isLoading: false,
    });

    renderStudioAt("/agents/wpid_del/studio?wr=wr_1");

    // The run-viewing surfaces are up (this used to render nothing at all).
    expect(screen.getByTestId("run-view-body")).toBeTruthy();
    expect(screen.getByTestId("browser-tab-body")).toBeTruthy();

    // Workflow-mutating surfaces degrade: legacy tag, no editor/copilot
    // bodies, blocked toggles, no save/run actions.
    expect(screen.getByText(/Agent deleted on/)).toBeTruthy();
    expect(screen.queryByTestId("editor-tab-body")).toBeNull();
    expect(
      screen.getAllByText("Source agent deleted — this run is view-only."),
    ).toHaveLength(2);
    expect(
      (screen.getByRole("button", { name: "Copilot" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Editor" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.queryByLabelText("Save workflow")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Run$/ })).toBeNull();
  });

  test("stays on the loading pulse while the run fallback is still loading", () => {
    workflowQueryMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    runQueryMock.mockReturnValue({ data: undefined, isLoading: true });

    renderStudioAt("/agents/wpid_del/studio?wr=wr_1");

    expect(screen.queryByTestId("run-view-body")).toBeNull();
    expect(screen.queryByText(/Agent deleted on/)).toBeNull();
  });

  test("a healthy workflow keeps the full studio (no deleted degradation)", () => {
    workflowQueryMock.mockReturnValue({
      data: liveWorkflow,
      isLoading: false,
      isError: false,
    });
    runQueryMock.mockReturnValue({
      data: makeRun(liveWorkflow),
      isLoading: false,
    });

    // Editor open alongside the run: the authoring cluster (Save) is up too.
    renderStudioAt("/agents/wpid_live/studio?wr=wr_1&panes=editor,overview");

    expect(screen.getByTestId("editor-tab-body")).toBeTruthy();
    expect(screen.queryByText(/Agent deleted on/)).toBeNull();
    expect(
      screen.queryByText("Source agent deleted — this run is view-only."),
    ).toBeNull();
    expect(screen.getByLabelText("Save workflow")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Copilot" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  test("a bare run link (watch-and-review) keeps Run but not the authoring cluster", () => {
    workflowQueryMock.mockReturnValue({
      data: liveWorkflow,
      isLoading: false,
      isError: false,
    });
    runQueryMock.mockReturnValue({
      data: makeRun(liveWorkflow),
      isLoading: false,
    });

    renderStudioAt("/agents/wpid_live/studio?wr=wr_1");

    expect(screen.getByRole("button", { name: /^Run$/ })).toBeTruthy();
    expect(screen.queryByLabelText("Save workflow")).toBeNull();
    expect(screen.queryByRole("button", { name: "Inputs" })).toBeNull();
    // Not the deleted-agent degradation: Editor is still openable.
    expect(
      (screen.getByRole("button", { name: "Editor" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});

describe("Workspace deferred draft cleanup", () => {
  test("preserves another workflow's deferred field and title through an intervening unmount", () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    clearDeferredEdits();
    copilotChatModule.canonicalRecoveriesByWorkflow.clear();
    const chat = vi
      .spyOn(copilotChatModule, "WorkflowCopilotChat")
      .mockImplementation(() => null);
    const workflowA = liveWorkflow as WorkflowApiResponse;
    const workflowB = {
      ...workflowA,
      workflow_permanent_id: "wpid_live_other",
      title: "Other workflow",
    };
    const fieldKey = JSON.stringify([
      workflowA.workflow_permanent_id,
      "node-1",
      "label",
    ]);
    const titleKey = `${workflowA.workflow_permanent_id}:title`;
    const onChange = vi.fn();
    const fieldProps = { value: "original", onChange, deferKey: fieldKey };
    const titleWrapper = ({ children }: { children: ReactNode }) => (
      <WorkflowPermanentIdContext.Provider
        value={workflowA.workflow_permanent_id}
      >
        {children}
      </WorkflowPermanentIdContext.Provider>
    );
    workflowQueryMock.mockReturnValue({ data: workflowA, isLoading: false });
    const first = renderSettingsWorkspace(workflowA);
    const field = renderHook(() => useDeferredLockedEdit(fieldProps));
    const title = renderHook(useDeferredTitleEdit, { wrapper: titleWrapper });
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    act(() => field.result.current.onChange("retained field"));
    act(() => {
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    act(() => title.result.current.onTitleChange("Retained title"));
    field.unmount();
    title.unmount();
    first.unmount();
    first.client.clear();
    workflowQueryMock.mockReturnValue({ data: workflowB, isLoading: false });
    const other = renderSettingsWorkspace(workflowB);
    try {
      act(() => finishSaveTransaction(owner));
      const otherFieldKey = JSON.stringify([
        workflowB.workflow_permanent_id,
        "node-1",
        "label",
      ]);
      const otherTitleKey = `${workflowB.workflow_permanent_id}:title`;
      for (const key of [otherFieldKey, otherTitleKey]) {
        deferredEdits.set(key, {
          value: "discarded",
          propValue: "original",
          propChanged: false,
        });
      }
      other.unmount();
      expect(deferredEdits.has(otherFieldKey)).toBe(false);
      expect(deferredEdits.has(otherTitleKey)).toBe(false);
      expect(deferredEdits.get(fieldKey)?.value).toBe("retained field");
      expect(deferredEdits.get(titleKey)?.value).toBe("Retained title");
      expect(onChange).not.toHaveBeenCalled();
      workflowQueryMock.mockReturnValue({ data: workflowA, isLoading: false });
      const revisited = renderSettingsWorkspace(workflowA);
      const revisitedField = renderHook(() =>
        useDeferredLockedEdit(fieldProps),
      );
      const revisitedTitle = renderHook(useDeferredTitleEdit, {
        wrapper: titleWrapper,
      });
      expect(onChange).toHaveBeenCalledExactlyOnceWith("retained field");
      expect(revisitedField.result.current.value).toBe("retained field");
      expect(useWorkflowTitleStore.getState().title).toBe("Retained title");
      expect(deferredEdits.has(fieldKey)).toBe(false);
      expect(deferredEdits.has(titleKey)).toBe(false);
      revisitedField.rerender();
      revisitedTitle.rerender();
      expect(onChange).toHaveBeenCalledOnce();
      revisitedField.unmount();
      revisitedTitle.unmount();
      revisited.unmount();
      revisited.client.clear();
    } finally {
      other.unmount();
      other.client.clear();
      chat.mockRestore();
      clearDeferredEdits();
      copilotChatModule.canonicalRecoveriesByWorkflow.clear();
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      vi.unstubAllGlobals();
    }
  });

  test.each(["none", "outgoing", "other", "adopted", "waiting"] as const)(
    "sweeps on unmount with %s recovery",
    (recovery) => {
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      clearDeferredEdits();
      copilotChatModule.canonicalRecoveriesByWorkflow.clear();
      const parkRecovery = (workflowPermanentId: string) => {
        copilotChatModule.canonicalRecoveriesByWorkflow.set(
          workflowPermanentId,
          { workflowPermanentId, waitingForUnlock: false, yaml: null },
        );
      };
      const chat = vi
        .spyOn(copilotChatModule, "WorkflowCopilotChat")
        .mockImplementation(function RecoveryChat() {
          useLayoutEffect(() => {
            return () => {
              if (recovery === "outgoing") parkRecovery("wpid_live");
            };
          }, []);
          return null;
        });
      workflowQueryMock.mockReturnValue({
        data: liveWorkflow,
        isLoading: false,
      });
      const view = renderSettingsWorkspace(liveWorkflow);
      try {
        deferredEdits.set(
          JSON.stringify(["wpid_live", "deleted-node", "label"]),
          {
            value: "draft",
            propValue: "original",
            propChanged: false,
          },
        );
        if (recovery === "other") parkRecovery("wpid_other");
        if (recovery === "adopted") {
          act(() => {
            beginCopilotAcceptance();
          });
        }
        if (recovery === "waiting") {
          act(() => {
            useWorkflowYamlEditorStore.setState({
              commitInProgress: true,
              lockKind: "save",
            });
          });
        }
        view.unmount();
        expect(
          deferredEdits.has(
            JSON.stringify(["wpid_live", "deleted-node", "label"]),
          ),
        ).toBe(recovery !== "none" && recovery !== "other");
        if (recovery === "outgoing") {
          expect(
            copilotChatModule.canonicalRecoveriesByWorkflow.has("wpid_live"),
          ).toBe(true);
        }
      } finally {
        view.unmount();
        view.client.clear();
        chat.mockRestore();
        clearDeferredEdits();
        copilotChatModule.canonicalRecoveriesByWorkflow.clear();
        useWorkflowYamlEditorStore.setState(
          useWorkflowYamlEditorStore.getInitialState(),
        );
        vi.unstubAllGlobals();
      }
    },
  );
});

describe("editor and legacy debugger parameter hydration", () => {
  test("initializes the next editor clean after leaving a dirty workflow with a pending save", () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    const previousWorkflow = {
      ...(liveWorkflow as WorkflowApiResponse),
      workflow_permanent_id: "wpid_previous",
      title: "Previous title",
      description: "Previous description",
    };
    workflowQueryMock.mockReturnValue({
      data: previousWorkflow,
      isLoading: false,
    });
    const previousWorkspace = renderSettingsWorkspace(previousWorkflow);
    const previousParameters = [
      {
        key: "previous_input",
        parameterType: "context" as const,
        sourceParameterKey: "source",
      },
    ];
    useWorkflowParametersStore.getState().setParameters(previousParameters);
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    previousWorkspace.unmount();
    previousWorkspace.client.clear();
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
    const nextWorkflow = {
      ...(liveWorkflow as WorkflowApiResponse),
      description: "Next description",
    };
    workflowQueryMock.mockReturnValue({
      data: {
        ...nextWorkflow,
        workflow_definition: {
          blocks: [],
          parameters: [
            {
              key: "next_input",
              parameter_type: "context",
              source: { key: "source" },
            },
          ],
        },
      },
      isLoading: false,
    });
    const editor = renderStudioAt("/agents/wpid_live/studio");
    const nextWorkspace = renderSettingsWorkspace(nextWorkflow);
    try {
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: nextWorkflow.title,
        description: nextWorkflow.description,
      });
      act(() => finishSaveTransaction(owner));
      expect(useWorkflowParametersStore.getState().parameters).toEqual([
        {
          key: "next_input",
          parameterType: "context",
          sourceParameterKey: "source",
        },
      ]);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: nextWorkflow.title,
        description: nextWorkflow.description,
        titleWorkflowPermanentId: "wpid_live",
        descriptionWorkflowPermanentId: "wpid_live",
      });
      expect(useWorkflowHasChangesStore.getState().getSaveData()).toMatchObject(
        {
          title: nextWorkflow.title,
          description: nextWorkflow.description,
        },
      );
      const editedParameters = [
        {
          key: "edited_input",
          parameterType: "context" as const,
          sourceParameterKey: "source",
        },
      ];
      const nextOwner = createYamlCommitOwner("wpid_live");
      act(() => {
        useWorkflowParametersStore.getState().setParameters(editedParameters);
        useWorkflowHasChangesStore.getState().setHasChanges(true);
        registerEditorOwner(nextOwner);
        expect(beginSaveTransaction(nextOwner)).toBe(true);
      });
      act(() => {
        useWorkflowHasChangesStore
          .getState()
          .setHasChanges(false, { fromYamlCommit: true });
        finishSaveTransaction(nextOwner);
      });
      expect(useWorkflowParametersStore.getState().parameters).toEqual(
        editedParameters,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    } finally {
      editor.unmount();
      nextWorkspace.unmount();
      nextWorkspace.client.clear();
      vi.unstubAllGlobals();
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
    }
  });
  test("loads authoritative workflows and preserves edits across delayed refetches", () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    const response = (id: string, key: string) =>
      ({
        workflow_permanent_id: id,
        workflow_definition: {
          parameters: [
            { key, parameter_type: "context", source: { key: "source" } },
          ],
        },
      }) as WorkflowApiResponse;
    const initial = response("wpid_1", "initial");
    const { rerender } = renderHook(
      ({ workflow, id }) => useHydrateWorkflowParameters(workflow, id),
      {
        initialProps: { workflow: initial, id: "wpid_1" },
      },
    );
    const key = () => useWorkflowParametersStore.getState().parameters[0]?.key;
    expect(key()).toBe("initial");
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    useWorkflowParametersStore.getState().setParameters([
      {
        key: "edited",
        parameterType: "context",
        sourceParameterKey: "source",
      },
    ]);
    rerender({ workflow: response("wpid_1", "refetched"), id: "wpid_1" });
    expect(key()).toBe("edited");
    rerender({ workflow: initial, id: "wpid_2" });
    expect(key()).toBe("edited");
    rerender({ workflow: response("wpid_2", "next"), id: "wpid_2" });
    expect(key()).toBe("next");
    useWorkflowHasChangesStore.getState().setHasChanges(false);
    rerender({ workflow: response("wpid_2", "after-save"), id: "wpid_2" });
    expect(key()).toBe("next");
  });
});

describe("save failures stop navigation and block runs", () => {
  test.each(["save", "copilot"] as const)(
    "retries a targeted layout once after a %s lock with the latest graph",
    async (lockKind) => {
      vi.stubGlobal(
        "ResizeObserver",
        class {
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      workflowQueryMock.mockReturnValue({
        data: liveWorkflow,
        isLoading: false,
      });
      const initialized = vi
        .spyOn(xyflowModule, "useNodesInitialized")
        .mockReturnValue(true);
      const layout = vi.spyOn(workflowEditorUtilsModule, "layout");
      const setNodes = vi.fn();
      const setEdges = vi.fn();
      const initialNodes: AppNode[] = [
        { id: "node_1", type: "nodeAdder", position: { x: 0, y: 0 }, data: {} },
      ];
      let replaceNodes!: (nodes: AppNode[]) => void;
      let targetBlock!: () => void;
      function Canvas() {
        const [nodes, updateNodes] = useState(initialNodes);
        const navigate = useNavigate();
        replaceNodes = updateNodes;
        targetBlock = () => navigate("/agents/wpid_live/edit/target");
        return (
          <FlowRenderer
            nodes={nodes}
            edges={[]}
            setNodes={setNodes}
            setEdges={setEdges}
            onNodesChange={vi.fn()}
            onEdgesChange={vi.fn()}
            initialTitle="Live agent"
            workflow={liveWorkflow}
          />
        );
      }
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      vi.useFakeTimers();
      const view = render(
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
            <ReactFlowProvider>
              <DebugStoreContext.Provider
                value={{ isDebugMode: false, blockRunsEnabled: false }}
              >
                <Routes>
                  <Route
                    path="/agents/:workflowPermanentId/edit/:blockLabel?"
                    element={<Canvas />}
                  />
                </Routes>
              </DebugStoreContext.Provider>
            </ReactFlowProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      try {
        await act(async () => vi.advanceTimersByTimeAsync(400));
        const owner = createYamlCommitOwner("wpid_live");
        let reservation: symbol | null = null;
        act(() => {
          registerEditorOwner(owner);
          if (lockKind === "save")
            expect(beginSaveTransaction(owner)).toBe(true);
          else reservation = beginCopilotAcceptance();
        });
        layout.mockClear();
        setNodes.mockClear();
        act(targetBlock);
        expect(layout).not.toHaveBeenCalled();
        const latest = [...initialNodes, { ...initialNodes[0]!, id: "node_2" }];
        act(() => replaceNodes(latest));
        expect(layout).not.toHaveBeenCalled();
        act(() => {
          if (reservation) finishCopilotAcceptance(reservation);
          else finishSaveTransaction(owner);
        });
        expect(layout).toHaveBeenCalledExactlyOnceWith(latest, [], "target");
        expect(setNodes).toHaveBeenCalledOnce();
        act(() => replaceNodes([...latest]));
        expect(layout).toHaveBeenCalledOnce();
      } finally {
        view.unmount();
        client.clear();
        initialized.mockRestore();
        layout.mockRestore();
        vi.useRealTimers();
      }
    },
  );
  afterEach(() => {
    navBlocker.state = "unblocked";
    navBlocker.proceed.mockReset();
    saveSpy.mockReset();
    vi.unstubAllGlobals();
  });
  test.each(
    (["save", "yaml", "copilot"] as const).flatMap((lockKind) =>
      (["unchanged", "replaced", "restored"] as const).map((propChange) => ({
        lockKind,
        propChange,
      })),
    ),
  )(
    "preserves buffered edits across a $lockKind lock with $propChange props",
    async ({ lockKind, propChange }) => {
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      vi.stubGlobal("IntersectionObserver", undefined);
      const { CodeEditor } = await vi.importActual<
        typeof import("../components/CodeEditor")
      >("../components/CodeEditor");
      const initialNode: AppNode = {
        id: "buffered",
        type: "codeBlock",
        position: { x: 0, y: 0 },
        data: {
          ...codeBlockNodeDefaultData,
          label: "buffered",
          code: "original",
          prompt: "original",
        },
      };
      let replaceNode: (value: string) => void;
      function BufferedEditors() {
        const [nodes, setNodes] = useState<AppNode[]>([initialNode]);
        replaceNode = (value) =>
          setNodes([
            {
              ...initialNode,
              data: {
                ...initialNode.data,
                prompt: value,
                code: value,
                label: value,
              },
            } as AppNode,
          ]);
        const node = nodes[0]!;
        if (node.type !== "codeBlock") throw new Error("Expected a code block");
        const update = (field: string, value: string) =>
          setNodes((current) =>
            applyNodeChanges(
              filterWorkflowChanges<NodeChange<AppNode>>([
                {
                  type: "replace",
                  id: node.id,
                  item: {
                    ...current[0]!,
                    data: { ...current[0]!.data, [field]: value },
                  } as AppNode,
                },
              ]),
              current,
            ),
          );
        return (
          <>
            <WorkflowBlockInputTextarea
              nodeId={node.id}
              aria-label="Buffered prompt"
              value={String(node.data.prompt)}
              onChange={(value) => update("prompt", value)}
              hideActions
            />
            <WorkflowBlockInput
              nodeId={node.id}
              aria-label="Buffered label"
              value={node.data.label}
              onChange={(value) => update("label", value)}
              hideParameterSelect
            />
            <CodeEditor
              value={String(node.data.code)}
              onChange={(value) => update("code", value)}
            />
            <output data-testid="saved-block">
              {JSON.stringify(getWorkflowBlocks(nodes, []))}
            </output>
          </>
        );
      }
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <ReactFlowProvider>
            <WorkflowScopeContext.Provider
              value={{ workflowId: "wpid_buffered", readOnly: false }}
            >
              <BufferedEditors />
            </WorkflowScopeContext.Provider>
          </ReactFlowProvider>
        </QueryClientProvider>,
      );
      const prompt = screen.getByRole("textbox", {
        name: "Buffered prompt",
      }) as HTMLTextAreaElement;
      const code = screen.getByRole("textbox", {
        name: "Buffered code",
      }) as HTMLTextAreaElement;
      const label = screen.getByRole("textbox", {
        name: "Buffered label",
      }) as HTMLInputElement;
      const saved = () =>
        JSON.parse(screen.getByTestId("saved-block").textContent!)[0] as {
          prompt: string;
          code: string;
          label: string;
        };
      const owner = createYamlCommitOwner("wpid_buffered");
      registerEditorOwner(owner);
      let token: symbol | null = null;
      vi.useFakeTimers();
      try {
        fireEvent.change(prompt, { target: { value: "pending prompt" } });
        fireEvent.change(code, { target: { value: "pending code" } });
        expect(prompt.value).toBe("pending prompt");
        expect(code.value).toBe("pending code");
        act(() => {
          if (lockKind === "save")
            expect(beginSaveTransaction(owner)).toBe(true);
          else if (lockKind === "yaml")
            expect(beginYamlCommit(owner)).toBe(true);
          else {
            token = beginCopilotAcceptance();
            expect(token).not.toBeNull();
          }
          expect(saved()).toMatchObject({
            prompt: "original",
            code: "original",
            label: "buffered",
          });
        });
        expect(prompt.value).toBe("pending prompt");
        expect(code.value).toBe("pending code");
        for (const field of [prompt, code, label]) {
          fireEvent.change(field, { target: { value: "refused" } });
          fireEvent.blur(field);
        }
        act(() => vi.advanceTimersByTime(400));
        expect(saved()).toMatchObject({
          prompt: "original",
          code: "original",
          label: "buffered",
        });
        expect(prompt.value).toBe("pending prompt");
        expect(code.value).toBe("pending code");
        expect(label.value).toBe("buffered");
        if (propChange !== "unchanged") {
          act(() => replaceNode("replacement"));
          expect(prompt.value).toBe("pending prompt");
          expect(code.value).toBe("pending code");
          expect(label.value).toBe("replacement");
          if (propChange === "restored") {
            act(() => replaceNode("original"));
          }
        }
        expect(prompt.disabled).toBe(true);
        expect(label.disabled).toBe(true);
        expect(code.readOnly).toBe(true);
        act(() => {
          if (token) finishCopilotAcceptance(token);
          else if (lockKind === "yaml") finishYamlCommit(owner);
          else finishSaveTransaction(owner);
        });
        const expectedValue =
          propChange === "replaced" ? "replacement" : "original";
        expect(saved()).toMatchObject({
          prompt: propChange === "replaced" ? expectedValue : "pending prompt",
          code: propChange === "replaced" ? expectedValue : "pending code",
          label: propChange === "unchanged" ? "buffered" : expectedValue,
        });
        act(() => vi.advanceTimersByTime(400));
        expect(prompt.value).toBe(saved().prompt);
        expect(code.value).toBe(saved().code);
        expect(label.value).toBe(saved().label);
        for (const field of [prompt, code, label])
          fireEvent.change(field, { target: { value: "accepted" } });
        act(() => vi.advanceTimersByTime(400));
        expect(saved()).toMatchObject({
          prompt: "accepted",
          code: "accepted",
          label: "accepted",
        });
        expect([prompt.value, code.value, label.value]).toEqual([
          saved().prompt,
          saved().code,
          saved().label,
        ]);
      } finally {
        cleanup();
        client.clear();
        vi.useRealTimers();
        vi.unstubAllGlobals();
        useWorkflowYamlEditorStore.setState(
          useWorkflowYamlEditorStore.getInitialState(),
        );
      }
    },
  );
  test("disables settings during a save and persists a webhook edit only after unlocking", async () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    const workflow = {
      ...(liveWorkflow as WorkflowApiResponse),
      webhook_callback_url: "https://example.test/original",
    };
    workflowQueryMock.mockReturnValue({ data: workflow, isLoading: false });
    const view = renderSettingsWorkspace(workflow);
    try {
      const webhook = screen.getByPlaceholderText(
        "https://",
      ) as HTMLInputElement;
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      act(() => expect(beginSaveTransaction(owner)).toBe(true));
      expect(webhook.matches(":disabled")).toBe(true);
      fireEvent.change(webhook, {
        target: { value: "https://example.test/refused" },
      });
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings
          .webhookCallbackUrl,
      ).toBe(workflow.webhook_callback_url);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      act(() => finishSaveTransaction(owner));
      expect(webhook.matches(":disabled")).toBe(false);
      expect(webhook.value).toBe(workflow.webhook_callback_url);
      fireEvent.change(webhook, {
        target: { value: "https://example.test/accepted" },
      });
      await waitFor(() =>
        expect(
          useWorkflowHasChangesStore.getState().getSaveData()!.settings
            .webhookCallbackUrl,
        ).toBe("https://example.test/accepted"),
      );
      const request = buildWorkflowSaveRequest(
        useWorkflowHasChangesStore.getState().getSaveData()!,
      );
      expect(request.webhook_callback_url).toBe(
        "https://example.test/accepted",
      );
      expect(request.webhook_callback_url).not.toBe(
        workflow.webhook_callback_url,
      );
    } finally {
      view.unmount();
      view.client.clear();
    }
  });
  test("invalid YAML prevents Save as Template from changing template status", async () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    workflowQueryMock.mockReturnValue({ data: liveWorkflow, isLoading: false });
    const put = vi.fn().mockResolvedValue({ data: {} });
    const post = vi.fn().mockResolvedValue({ data: [] });
    const clientSpy = vi
      .spyOn(axiosClientModule, "getClient")
      .mockResolvedValue({ put, post } as never);
    const renderer = vi
      .spyOn(flowRendererModule, "FlowRenderer")
      .mockImplementation(() => <></>);
    const chat = vi
      .spyOn(copilotChatModule, "WorkflowCopilotChat")
      .mockImplementation(() => null);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    try {
      render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <WorkflowPermanentIdContext.Provider value="wpid_live">
              <ReactFlowProvider>
                <DebugStoreContext.Provider
                  value={{ isDebugMode: false, blockRunsEnabled: false }}
                >
                  <Workspace
                    initialNodes={[]}
                    initialEdges={[]}
                    initialTitle="Live agent"
                    workflow={liveWorkflow}
                    embedded
                  />
                  <EditorOverflowMenu />
                </DebugStoreContext.Provider>
              </ReactFlowProvider>
            </WorkflowPermanentIdContext.Provider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      act(() => {
        useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
          workflow: liveWorkflow,
          title: "Live agent",
          description: null,
          settings: apiWorkflowToSettings(liveWorkflow),
          parameters: [],
          blocks: [],
          workflowDefinitionVersion: 2,
        }));
        useWorkflowHasChangesStore.getState().setHasChanges(true);
        useWorkflowYamlEditorStore.getState().open("blocks: []");
        useWorkflowYamlEditorStore
          .getState()
          .setDraft("workflow_definition: [");
      });
      fireEvent.keyDown(screen.getByRole("button", { name: "More actions" }), {
        key: "Enter",
      });
      fireEvent.click(
        await screen.findByRole("menuitem", { name: "Save as Template" }),
      );
      await waitFor(() =>
        expect(useWorkflowYamlEditorStore.getState().error).toBeTruthy(),
      );
      expect(put).not.toHaveBeenCalled();
      expect(post).not.toHaveBeenCalledWith(
        "/workflow/copilot/convert-yaml-to-blocks",
        expect.anything(),
      );
      expect(saveSpy).not.toHaveBeenCalled();
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({ title: "Saved as template" }),
      );
      expect(useWorkflowYamlEditorStore.getState().draft).toBe(
        "workflow_definition: [",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    } finally {
      cleanup();
      client.clear();
      clientSpy.mockRestore();
      renderer.mockRestore();
      chat.mockRestore();
    }
  });
  test.each(["header", "template"])(
    "contains ordinary save failures from the %s action",
    async (action) => {
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      workflowQueryMock.mockReturnValue({
        data: liveWorkflow,
        isLoading: false,
      });
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      const failure = new AxiosError("Network Error");
      const saveHook = vi
        .spyOn(saveWorkflowModule, "useSaveWorkflow")
        .mockReturnValue(async () => {
          throw failure;
        });
      const errors: unknown[] = [];
      const onUnhandled = (error: unknown) => errors.push(error);
      const logged: unknown[][] = [];
      const logging = vi
        .spyOn(console, "error")
        .mockImplementation((...args) => {
          logged.push(args);
        });
      process.on("unhandledRejection", onUnhandled);
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <ReactFlowProvider>
              <DebugStoreContext.Provider
                value={{ isDebugMode: false, blockRunsEnabled: false }}
              >
                {action === "header" ? (
                  <workflowHeaderModule.SaveButton />
                ) : (
                  <EditorOverflowMenu />
                )}
              </DebugStoreContext.Provider>
            </ReactFlowProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      try {
        if (action === "header") fireEvent.click(screen.getByRole("button"));
        else {
          fireEvent.keyDown(
            screen.getByRole("button", { name: "More actions" }),
            { key: "Enter" },
          );
          fireEvent.click(
            await screen.findByRole("menuitem", { name: "Save as Template" }),
          );
        }
        await act(async () => {
          await new Promise((resolve) => setTimeout(resolve, 0));
        });
        expect(errors).toEqual([]);
        expect(logged.some((args) => args.includes(failure))).toBe(true);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      } finally {
        process.off("unhandledRejection", onUnhandled);
        logging.mockRestore();
        saveHook.mockRestore();
        view.unmount();
        client.clear();
      }
    },
  );
  test("template creation waits for a successful save and preserves refused or stale edits", async () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    workflowQueryMock.mockReturnValue({ data: liveWorkflow, isLoading: false });
    const put = vi.fn().mockResolvedValue({ data: {} });
    const clientSpy = vi
      .spyOn(axiosClientModule, "getClient")
      .mockResolvedValue({ put } as never);
    const save = vi.fn<() => Promise<void>>();
    const saveHook = vi
      .spyOn(saveWorkflowModule, "useSaveWorkflow")
      .mockReturnValue(save);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const menu = () => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReactFlowProvider>
            <DebugStoreContext.Provider
              value={{ isDebugMode: false, blockRunsEnabled: false }}
            >
              <EditorOverflowMenu />
            </DebugStoreContext.Provider>
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const control = render(menu());
    const selectTemplate = async (name = "Save as Template") => {
      fireEvent.keyDown(control.getByRole("button", { name: "More actions" }), {
        key: "Enter",
      });
      fireEvent.click(await screen.findByRole("menuitem", { name }));
      await waitFor(() =>
        expect(screen.queryByRole("menuitem", { name })).toBeNull(),
      );
    };
    try {
      for (const error of [new SaveRefusedError(), new SaveStaleError()]) {
        let reject!: (error: Error) => void;
        save.mockImplementationOnce(
          () =>
            new Promise((_resolve, rejectSave) => {
              reject = rejectSave;
            }),
        );
        await selectTemplate();
        expect(put).not.toHaveBeenCalled();
        await act(async () => {
          reject(error);
        });
        expect(put).not.toHaveBeenCalled();
        expect(toast).not.toHaveBeenCalledWith(
          expect.objectContaining({ title: "Saved as template" }),
        );
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      }
      let resolve!: () => void;
      save.mockImplementationOnce(
        () =>
          new Promise((resolveSave) => {
            resolve = resolveSave;
          }),
      );
      await selectTemplate();
      expect(put).not.toHaveBeenCalled();
      await act(async () => {
        resolve();
      });
      await waitFor(() => expect(put).toHaveBeenCalledOnce());
      expect(put).toHaveBeenLastCalledWith(
        expect.stringContaining("is_template=true"),
      );
      await waitFor(() =>
        expect(toast).toHaveBeenCalledWith({
          title: "Saved as template",
          variant: "success",
        }),
      );
      workflowQueryMock.mockReturnValue({
        data: { ...(liveWorkflow as WorkflowApiResponse), is_template: true },
        isLoading: false,
      });
      control.rerender(menu());
      await selectTemplate("Remove from Templates");
      await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
      expect(put).toHaveBeenLastCalledWith(
        expect.stringContaining("is_template=false"),
      );
      expect(save).toHaveBeenCalledTimes(3);
    } finally {
      control.unmount();
      client.clear();
      saveHook.mockRestore();
      clientSpy.mockRestore();
    }
  });
  test("comparison acceptance keeps the proposal during a save and applies it on retry", async () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    workflowQueryMock.mockReturnValue({ data: liveWorkflow, isLoading: false });
    let chat!: NonNullable<
      Parameters<typeof copilotChatModule.WorkflowCopilotChat>[0]
    >;
    const chatSpy = vi
      .spyOn(copilotChatModule, "WorkflowCopilotChat")
      .mockImplementation((props) => {
        chat = props!;
        return null;
      });
    const panelSpy = vi
      .spyOn(comparisonPanelModule, "WorkflowComparisonPanel")
      .mockImplementation(() => <div data-testid="comparison" />);
    let canvas!: flowRendererModule.FlowRendererProps;
    const renderer = vi
      .spyOn(flowRendererModule, "FlowRenderer")
      .mockImplementation((props) => {
        canvas = props;
        return <></>;
      });
    const post = vi.fn().mockResolvedValue({
      data: { workflow_definition: { blocks: [], parameters: [] } },
    });
    const clientSpy = vi
      .spyOn(axiosClientModule, "getClient")
      .mockResolvedValue({ post } as never);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const clearPending = vi.fn();
    const proposal = {
      ...(liveWorkflow as WorkflowApiResponse),
      title: "Accepted proposal",
      description: "Proposal description",
    };
    const initialNode: AppNode = {
      id: "node_1",
      type: "codeBlock",
      position: { x: 0, y: 0 },
      data: { ...codeBlockNodeDefaultData, label: "original" },
    };
    try {
      render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <ReactFlowProvider>
              <Workspace
                initialNodes={[initialNode]}
                initialEdges={[]}
                initialTitle="Live agent"
                workflow={liveWorkflow}
                embedded
              />
            </ReactFlowProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      act(() =>
        useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
          workflow: liveWorkflow,
          title: "Live agent",
          description: null,
          settings: apiWorkflowToSettings(liveWorkflow),
          parameters: [],
          blocks: [],
          workflowDefinitionVersion: 2,
        })),
      );
      await act(async () => {
        await chat!.onReviewWorkflow!(proposal, clearPending, vi.fn());
      });
      expect(screen.getByTestId("comparison")).toBeTruthy();
      const review = useWorkflowPanelStore.getState().workflowPanelState.data!;
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      act(() => {
        expect(beginSaveTransaction(owner)).toBe(true);
      });
      const revision = useWorkflowYamlEditorStore.getState().revision;
      vi.mocked(toast).mockClear();
      await act(async () => {
        await review.onCopilotReviewClose!("approve");
      });
      expect(clearPending).not.toHaveBeenCalled();
      expect(useWorkflowPanelStore.getState().workflowPanelState.data).toBe(
        review,
      );
      expect(screen.getByTestId("comparison")).toBeTruthy();
      expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
      expect(toast).toHaveBeenCalledExactlyOnceWith({
        title: "A save is in progress",
        variant: "destructive",
      });
      act(() => finishSaveTransaction(owner));
      await act(async () => {
        await review.onCopilotReviewClose!("approve");
      });
      expect(clearPending).toHaveBeenCalledOnce();
      expect(screen.queryByTestId("comparison")).toBeNull();
      expect(canvas.nodes.some((node) => node.id === initialNode.id)).toBe(
        false,
      );
      expect(useWorkflowTitleStore.getState().title).toBe(proposal.title);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      const hiddenParameter = {
        parameter_type: "aws_secret" as const,
        key: "api_key",
        aws_key: "edited_reference",
        workflow_id: proposal.workflow_id,
        aws_secret_parameter_id: "aws_secret_local",
        created_at: "2026-09-01T00:00:00Z",
        modified_at: "2026-09-01T00:00:00Z",
        deleted_at: null,
        description: "Local reference",
      };
      act(() =>
        chat.onWorkflowUpdate!(
          {
            ...proposal,
            workflow_definition: {
              ...proposal.workflow_definition,
              parameters: [hiddenParameter],
            },
          },
          { applied: true },
        ),
      );
      const snapshot = chat.captureEditorState!();
      act(() => chat.onWorkflowUpdate!(proposal, { midTurnDraft: true }));
      expect(canvas.parameterBaseline).toEqual(
        proposal.workflow_definition.parameters,
      );
      act(() => expect(chat.restoreEditorState!(snapshot!)).toBe("restored"));
      expect(canvas.parameterBaseline).toEqual([hiddenParameter]);
    } finally {
      cleanup();
      client.clear();
      chatSpy.mockRestore();
      panelSpy.mockRestore();
      renderer.mockRestore();
      clientSpy.mockRestore();
      useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
    }
  });
  test.each([
    ["save", beginSaveTransaction, "A save is in progress"],
    ["YAML commit", beginYamlCommit, "A YAML commit is in progress"],
  ] as const)(
    "Workspace preserves passive changes and its revision during a %s",
    (_kind, beginTransaction, lockMessage) => {
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      workflowQueryMock.mockReturnValue({
        data: liveWorkflow,
        isLoading: false,
      });
      const initialNode: AppNode = {
        id: "node_1",
        type: "codeBlock",
        position: { x: 0, y: 0 },
        data: { ...codeBlockNodeDefaultData, label: "original" },
      };
      let canvas!: flowRendererModule.FlowRendererProps;
      const renderer = vi
        .spyOn(flowRendererModule, "FlowRenderer")
        .mockImplementation((props) => {
          canvas = props;
          return <></>;
        });
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      try {
        render(
          <QueryClientProvider client={client}>
            <MemoryRouter>
              <ReactFlowProvider>
                <Workspace
                  initialNodes={[initialNode]}
                  initialEdges={[]}
                  initialTitle="Live agent"
                  workflow={liveWorkflow}
                  embedded
                />
              </ReactFlowProvider>
            </MemoryRouter>
          </QueryClientProvider>,
        );
        expect(canvas.nodes).toEqual([initialNode]);
        const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
        act(() => expect(beginTransaction(owner)).toBe(true));
        const revision = useWorkflowYamlEditorStore.getState().revision;
        vi.mocked(toast).mockClear();
        const allowed: NodeChange<AppNode>[] = [
          { type: "select", id: "node_1", selected: true },
          {
            type: "dimensions",
            id: "node_1",
            dimensions: { width: 100, height: 50 },
          },
          { type: "position", id: "node_1", position: { x: 10, y: 10 } },
          {
            type: "position",
            id: "node_1",
            position: { x: 20, y: 20 },
            dragging: true,
          },
          {
            type: "position",
            id: "node_1",
            position: { x: 30, y: 30 },
            dragging: false,
          },
        ];
        for (const change of allowed) {
          act(() => canvas.onNodesChange([change]));
          expect(toast).not.toHaveBeenCalled();
          expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
        }
        expect(canvas.nodes).toEqual([
          {
            ...initialNode,
            selected: true,
            measured: { width: 100, height: 50 },
            position: { x: 30, y: 30 },
            dragging: false,
          },
        ]);
        const editedNode: AppNode = {
          ...initialNode,
          data: { ...codeBlockNodeDefaultData, label: "edited" },
        };
        const refused: NodeChange<AppNode>[] = [
          { type: "add", item: { ...editedNode, id: "node_2" } },
          { type: "remove", id: "node_1" },
          { type: "replace", id: "node_1", item: editedNode },
        ];
        for (const change of refused) {
          const before = canvas.nodes;
          const selected = !before[0]!.selected;
          vi.mocked(toast).mockClear();
          act(() =>
            canvas.onNodesChange([
              change,
              { type: "select", id: "node_1", selected },
            ]),
          );
          expect(canvas.nodes).toEqual([{ ...before[0], selected }]);
          expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
          expect(toast).toHaveBeenCalledExactlyOnceWith({
            title: lockMessage,
            variant: "destructive",
          });
        }
        const before = canvas.nodes;
        vi.mocked(toast).mockClear();
        act(() => canvas.onNodesChange(refused));
        expect(canvas.nodes).toBe(before);
        expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
        expect(toast).toHaveBeenCalledExactlyOnceWith({
          title: lockMessage,
          variant: "destructive",
        });
        act(() => finishSaveTransaction(owner));
        vi.mocked(toast).mockClear();
        act(() => canvas.onNodesChange(allowed));
        expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
        act(() =>
          canvas.onNodesChange([
            { type: "replace", id: "node_1", item: editedNode },
          ]),
        );
        expect(canvas.nodes).toEqual([editedNode]);
        expect(useWorkflowYamlEditorStore.getState().revision).toBe(
          revision + 1,
        );
        expect(toast).not.toHaveBeenCalled();
      } finally {
        cleanup();
        renderer.mockRestore();
        client.clear();
      }
    },
  );
  test("allows canvas-only changes during a save and refuses only persisted edits in mixed batches", () => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    workflowQueryMock.mockReturnValue({ data: liveWorkflow, isLoading: false });
    const onNodesChange = vi.fn();
    const canvasNodes: AppNode[] = [
      { id: "node_1", type: "nodeAdder", position: { x: 0, y: 0 }, data: {} },
    ];
    let flowStore!: ReturnType<typeof useStoreApi<AppNode>>;
    function Canvas() {
      flowStore = useStoreApi<AppNode>();
      return (
        <FlowRenderer
          nodes={canvasNodes}
          edges={[]}
          setNodes={vi.fn()}
          setEdges={vi.fn()}
          onNodesChange={onNodesChange}
          onEdgesChange={vi.fn()}
          initialTitle="Live agent"
          workflow={liveWorkflow}
        />
      );
    }
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
          <ReactFlowProvider>
            <DebugStoreContext.Provider
              value={{ isDebugMode: false, blockRunsEnabled: false }}
            >
              <Canvas />
            </DebugStoreContext.Provider>
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const owner = createYamlCommitOwner("wpid_live");
    act(() => {
      registerEditorOwner(owner);
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    onNodesChange.mockClear();
    vi.mocked(toast).mockClear();
    const allowed: NodeChange<AppNode>[] = [
      { type: "select", id: "node_1", selected: true },
      {
        type: "dimensions",
        id: "node_1",
        dimensions: { width: 100, height: 50 },
      },
      { type: "position", id: "node_1", position: { x: 10, y: 10 } },
      {
        type: "position",
        id: "node_1",
        position: { x: 20, y: 20 },
        dragging: true,
      },
      {
        type: "position",
        id: "node_1",
        position: { x: 30, y: 30 },
        dragging: false,
      },
    ];
    act(() => flowStore.getState().triggerNodeChanges(allowed));
    expect(onNodesChange).toHaveBeenCalledExactlyOnceWith(allowed);
    expect(toast).not.toHaveBeenCalled();
    const editedNode: AppNode = {
      id: "node_1",
      type: "codeBlock",
      position: { x: 0, y: 0 },
      data: { ...codeBlockNodeDefaultData, label: "edited" },
    };
    const refused: NodeChange<AppNode>[] = [
      { type: "add", item: editedNode },
      { type: "remove", id: "node_1" },
      { type: "replace", id: "node_1", item: editedNode },
    ];
    for (const change of refused) {
      onNodesChange.mockClear();
      vi.mocked(toast).mockClear();
      act(() => flowStore.getState().triggerNodeChanges([change, ...allowed]));
      expect(onNodesChange).toHaveBeenCalledExactlyOnceWith(allowed);
      expect(toast).toHaveBeenCalledExactlyOnceWith({
        title: "A save is in progress",
        variant: "destructive",
      });
    }
    onNodesChange.mockClear();
    act(() => flowStore.getState().triggerNodeChanges(refused));
    expect(onNodesChange).not.toHaveBeenCalled();
    act(() => finishSaveTransaction(owner));
    onNodesChange.mockClear();
    vi.mocked(toast).mockClear();
    act(() => flowStore.getState().triggerNodeChanges(refused));
    expect(onNodesChange).toHaveBeenCalledExactlyOnceWith(refused);
    expect(toast).not.toHaveBeenCalled();
    cleanup();
    client.clear();
  });
  test("saves a corrected required prompt through the navigation blocker before its debounce fires", async () => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowParametersStore.setState({ parameters: [] });
    const workflow = {
      ...(liveWorkflow as WorkflowApiResponse),
      workflow_definition: {
        version: 2,
        parameters: [],
        blocks: [
          {
            block_type: "navigation",
            label: "navigate",
            title: "Navigate",
            url: null,
            navigation_goal: "",
            parameters: [],
            error_code_mapping: null,
            complete_criterion: null,
            terminate_criterion: null,
            engine: null,
            include_action_history_in_verification: false,
            continue_on_failure: false,
            model: null,
            output_parameter: {
              parameter_type: "output",
              key: "navigate_output",
              description: null,
              output_parameter_id: "op_navigate",
              workflow_id: "wf_live",
              created_at: "2026-09-01T00:00:00Z",
              modified_at: "2026-09-01T00:00:00Z",
              deleted_at: null,
            },
          },
        ],
      },
    } as WorkflowApiResponse;
    workflowQueryMock.mockReturnValue({ data: workflow, isLoading: false });
    put.mockReset().mockResolvedValue({ data: workflow });
    navBlocker.state = "unblocked";
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const { nodes, edges } = getElements(
      workflow.workflow_definition.blocks,
      apiWorkflowToSettings(workflow),
      true,
    );
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const view = render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
          <WorkflowPermanentIdContext.Provider value="wpid_live">
            <ReactFlowProvider>
              <DebugStoreContext.Provider
                value={{ isDebugMode: false, blockRunsEnabled: false }}
              >
                <TooltipProvider>
                  <Workspace
                    initialNodes={nodes}
                    initialEdges={edges}
                    initialTitle={workflow.title}
                    workflow={workflow}
                    embedded
                  />
                  <NavigationEditor
                    blockId={
                      nodes.find((node) => node.type === "navigation")!.id
                    }
                  />
                </TooltipProvider>
              </DebugStoreContext.Provider>
            </ReactFlowProvider>
          </WorkflowPermanentIdContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    try {
      const prompt = view.container.querySelector<HTMLTextAreaElement>(
        'textarea[name="navigationGoal"]',
      );
      expect(prompt).not.toBeNull();
      vi.useFakeTimers();
      fireEvent.change(prompt!, {
        target: { value: "Open the dashboard" },
      });
      expect(
        workflowEditorUtilsModule.getWorkflowErrors(canvas.current!.nodes),
      ).toEqual(["navigate: Prompt is required."]);
      act(() => {
        navBlocker.state = "blocked";
        useWorkflowHasChangesStore.getState().setHasChanges(true);
      });
      vi.mocked(toast).mockClear();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      });
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive" }),
      );
      expect(put).toHaveBeenCalledOnce();
      expect(parse(put.mock.calls[0]![1])).toMatchObject({
        workflow_definition: {
          blocks: [
            {
              label: "navigate",
              navigation_goal: "Open the dashboard",
            },
          ],
        },
      });
      expect(navBlocker.proceed).toHaveBeenCalledOnce();
    } finally {
      view.unmount();
      client.clear();
      vi.useRealTimers();
      clearDeferredEdits();
    }
  });
  test.each([new SaveRefusedError(), new SaveStaleError()])(
    "keeps the navigation blocker after %s",
    async (error) => {
      vi.stubGlobal(
        "ResizeObserver",
        class {
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      integration.mockSave = true;
      saveSpy.mockRejectedValueOnce(error);
      navBlocker.state = "blocked";
      const workflow = {
        ...(liveWorkflow as WorkflowApiResponse),
        workflow_permanent_id: "wpid_live",
      };
      workflowQueryMock.mockReturnValue({ data: workflow, isLoading: false });
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
            <ReactFlowProvider>
              <DebugStoreContext.Provider
                value={{ isDebugMode: false, blockRunsEnabled: false }}
              >
                <FlowRenderer
                  nodes={[]}
                  edges={[]}
                  setNodes={vi.fn()}
                  setEdges={vi.fn()}
                  onNodesChange={vi.fn()}
                  onEdgesChange={vi.fn()}
                  initialTitle="Live agent"
                  workflow={workflow}
                />
              </DebugStoreContext.Provider>
            </ReactFlowProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      act(() => useWorkflowHasChangesStore.getState().setHasChanges(true));
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(saveSpy).toHaveBeenCalledOnce());
      expect(navBlocker.proceed).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Save changes" })).toBeTruthy();
      cleanup();
      client.clear();
    },
  );
  test.each([new SaveRefusedError(), new SaveStaleError()])(
    "does not start a block run after %s",
    async (error) => {
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowHasChangesStore.setState(
        useWorkflowHasChangesStore.getInitialState(),
      );
      integration.mockSave = true;
      saveSpy.mockRejectedValueOnce(error);
      workflowQueryMock.mockReturnValue({
        data: liveWorkflow,
        isLoading: false,
      });
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false },
          mutations: { retry: false },
        },
      });
      const fetch = vi.spyOn(client, "fetchQuery");
      render(
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
            <Routes>
              <Route
                path="/agents/:workflowPermanentId/*"
                element={
                  <ReactFlowProvider>
                    <DebugStoreContext.Provider
                      value={{ isDebugMode: true, blockRunsEnabled: true }}
                    >
                      <BlockActionContext.Provider
                        value={{
                          requestDeleteNodeCallback: vi.fn(),
                          duplicateNodeCallback: vi.fn(),
                          transmuteNodeCallback: vi.fn(),
                          toggleScriptForNodeCallback: vi.fn(),
                        }}
                      >
                        <NodeHeader
                          blockLabel="block_1"
                          editable
                          nodeId="node_1"
                          totpIdentifier={null}
                          totpUrl={null}
                          type="code"
                        />
                      </BlockActionContext.Provider>
                    </DebugStoreContext.Provider>
                  </ReactFlowProvider>
                }
              />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Run this block" })),
      );
      await waitFor(() => expect(saveSpy).toHaveBeenCalledOnce());
      expect(fetch).not.toHaveBeenCalled();
      cleanup();
      client.clear();
    },
  );
});

test("the editor browser stops indicating execution during a retry wait and resumes for the next attempt", () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState(
    useWorkflowHasChangesStore.getInitialState(),
  );
  const renderer = vi
    .spyOn(flowRendererModule, "FlowRenderer")
    .mockImplementation(function ReadyRenderer({
      onLayoutPhaseChange,
    }: FlowRendererProps) {
      useEffect(() => onLayoutPhaseChange?.("ready"), [onLayoutPhaseChange]);
      return <></>;
    });
  const header = vi
    .spyOn(workflowHeaderModule, "WorkflowHeader")
    .mockReturnValue(null);
  const chat = vi
    .spyOn(copilotChatModule, "WorkflowCopilotChat")
    .mockReturnValue(null);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const editor = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agents/wpid_live/edit?wr=wr_1"]}>
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/edit"
            element={
              <ReactFlowProvider>
                <Workspace
                  initialNodes={[]}
                  initialEdges={[]}
                  initialTitle="Live agent"
                  workflow={liveWorkflow}
                  showBrowser
                />
              </ReactFlowProvider>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  runQueryMock.mockReturnValue({
    data: {
      workflow_run_id: "wr_1",
      status: Status.Failed,
      retry_pending: true,
    },
  });
  const view = render(editor());
  expect(
    screen.getByTestId("browser-stream").getAttribute("data-executing"),
  ).toBe("false");

  runQueryMock.mockReturnValue({
    data: { workflow_run_id: "wr_1", status: Status.Running, attempt: 2 },
  });
  view.rerender(editor());
  expect(
    screen.getByTestId("browser-stream").getAttribute("data-executing"),
  ).toBe("true");
  view.unmount();
  queryClient.clear();
  renderer.mockRestore();
  header.mockRestore();
  chat.mockRestore();
  vi.unstubAllGlobals();
});

describe("Discover recording handoff", () => {
  beforeEach(() => {
    integration.canvas = false;
    debugQueryMock.mockReturnValue({ data: undefined });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    sessionStorage.clear();
    useRecordingStore.getState().reset();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
    activeRunQueryMock.mockReturnValue({
      data: { active_run_session_id: null },
    });
    workflowQueryMock.mockReturnValue({ data: liveWorkflow });
    runQueryMock.mockReturnValue({ data: undefined });
    historyGet.mockResolvedValue({ data: {} });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useRecordingStore.getState().reset();
  });

  test.each(["save", "accept", "authoring"])(
    "keeps the recording request until the %s hold clears",
    async (hold) => {
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const router = createMemoryRouter(
        [
          {
            path: "/agents/:workflowPermanentId/edit",
            element: (
              <ReactFlowProvider>
                <Workspace
                  initialNodes={[]}
                  initialEdges={[]}
                  initialTitle="Live agent"
                  workflow={liveWorkflow}
                />
              </ReactFlowProvider>
            ),
          },
        ],
        {
          initialEntries: [
            "/agents/wpid_live/edit?record=1&copilotHeadlessTurnDrain=1",
          ],
        },
      );
      const editor = () => (
        <QueryClientProvider client={client}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      );
      const view = render(editor());
      const setIsRecording = vi.spyOn(
        useRecordingStore.getState(),
        "setIsRecording",
      );
      act(() =>
        useWorkflowYamlEditorStore.setState({
          commitInProgress: hold === "save",
          copilotAcceptance: hold === "accept" ? Symbol("accept") : null,
          authoringInProgress: hold === "authoring",
        }),
      );
      debugQueryMock.mockReturnValue({
        data: { browser_session_id: "pbs_debug", status: "created" },
      });
      await act(async () => {
        await router.navigate(
          router.state.location.pathname + router.state.location.search,
          { replace: true },
        );
      });
      expect(setIsRecording).not.toHaveBeenCalled();
      expect(
        new URLSearchParams(router.state.location.search).get("record"),
      ).toBe("1");

      await act(async () =>
        useWorkflowYamlEditorStore.setState({
          commitInProgress: false,
          copilotAcceptance: null,
          authoringInProgress: false,
        }),
      );
      await waitFor(() => expect(setIsRecording).toHaveBeenCalledOnce());
      expect(useRecordingStore.getState().isRecording).toBe(true);
      expect(
        new URLSearchParams(router.state.location.search).has("record"),
      ).toBe(false);
      expect(
        new URLSearchParams(router.state.location.search).get(
          "copilotHeadlessTurnDrain",
        ),
      ).toBe("1");
      view.unmount();
      client.clear();
    },
  );
});

describe("save callback content revisions", () => {
  const saveData = (): WorkflowSaveData => ({
    workflow: liveWorkflow,
    title: useWorkflowTitleStore.getState().title,
    description: useWorkflowTitleStore.getState().description,
    settings: apiWorkflowToSettings(liveWorkflow),
    blocks: [],
    parameters: [],
    workflowDefinitionVersion: 2,
  });

  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowTitleStore.getState().setTitle("Live agent");
  });

  test("registers equivalent save data without advancing the revision", () => {
    const previous = saveData();
    const changes = useWorkflowHasChangesStore.getState();
    const revision = useWorkflowYamlEditorStore.getState().revision;
    changes.setGetSaveData(() => previous);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
    const next = () => ({
      ...saveData(),
      description: "",
      settings: {
        ...previous.settings,
        extraHttpHeaders: "{}",
        scriptCacheKey: "default",
      },
    });
    changes.setGetSaveData(next);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
    expect(useWorkflowHasChangesStore.getState().getSaveData).toBe(next);
  });

  test.each(["title", "description"] as const)(
    "advances the revision for a %s edit without another bump on registration",
    (field) => {
      const previous = saveData();
      const changes = useWorkflowHasChangesStore.getState();
      changes.setGetSaveData(() => previous);
      const revision = useWorkflowYamlEditorStore.getState().revision;
      const titles = useWorkflowTitleStore.getState();
      if (field === "title") {
        titles.setTitle("Edited title");
      } else {
        titles.setDescriptionFromUser("Edited description");
      }
      expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
      changes.setGetSaveData(saveData);
      expect(useWorkflowHasChangesStore.getState().getSaveData()?.[field]).toBe(
        `Edited ${field}`,
      );
      expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
      changes.setGetSaveData(() => ({ ...saveData() }));
      expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
    },
  );

  test("registers invalid settings and their repair without duplicating edit revisions", () => {
    const changes = useWorkflowHasChangesStore.getState();
    const previous = saveData();
    changes.setGetSaveData(() => previous);
    const invalid = () => ({
      ...previous,
      settings: { ...previous.settings, extraHttpHeaders: "{" },
    });
    const revision = useWorkflowYamlEditorStore.getState().revision;
    changes.setHasChanges(true);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
    expect(() => changes.setGetSaveData(invalid)).not.toThrow();
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
    expect(useWorkflowHasChangesStore.getState().getSaveData).toBe(invalid);
    changes.setHasChanges(true);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 2);
    const repaired = () => previous;
    changes.setGetSaveData(repaired);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 2);
    expect(useWorkflowHasChangesStore.getState().getSaveData).toBe(repaired);
  });
});

test("hydrates a revisited workflow before clearing dirty state when its disposed owner's PUT settles", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState(
    useWorkflowHasChangesStore.getInitialState(),
  );
  useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
  useWorkflowParametersStore.setState({ parameters: [] });
  navBlocker.state = "unblocked";
  const workflow = {
    ...(liveWorkflow as WorkflowApiResponse),
    workflow_id: "wf_before_save",
    title: "Before save",
    description: null,
    workflow_definition: { version: 2, blocks: [], parameters: [] },
  } as WorkflowApiResponse;
  const savedWorkflow = {
    ...workflow,
    workflow_id: "wf_after_save",
    version: 2,
    title: "Saved workflow",
    description: "Saved description",
    webhook_callback_url: "https://example.test/hook",
    workflow_definition: {
      version: 2,
      blocks: [{ block_type: "wait", label: "saved_wait", wait_sec: 9 }],
      parameters: [
        {
          parameter_type: "context",
          key: "saved_context",
          source: {
            parameter_type: "workflow",
            key: "input",
            workflow_parameter_type: "string",
          },
        },
      ],
    },
  } as WorkflowApiResponse;
  let resolveSave!: (response: { data: WorkflowApiResponse }) => void;
  const put = vi.fn().mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveSave = resolve;
      }),
  );
  const clientSpy = vi
    .spyOn(axiosClientModule, "getClient")
    .mockResolvedValue({ put } as never);
  const { useWorkflowSave } = await vi.importActual<
    typeof import("@/store/WorkflowHasChangesStore")
  >("@/store/WorkflowHasChangesStore");
  const saveHook = vi
    .spyOn(workflowHasChangesModule, "useWorkflowSave")
    .mockImplementation(useWorkflowSave);
  const OriginalRenderer = flowRendererModule.FlowRenderer;
  let canvas!: FlowRendererProps;
  const renderer = vi
    .spyOn(flowRendererModule, "FlowRenderer")
    .mockImplementation((props) => {
      if (!props.readOnly) canvas = props;
      return <OriginalRenderer {...props} />;
    });
  const header = vi
    .spyOn(workflowHeaderModule, "WorkflowHeader")
    .mockReturnValue(null);
  const chat = vi
    .spyOn(copilotChatModule, "WorkflowCopilotChat")
    .mockReturnValue(null);
  workflowQueryMock.mockReturnValue({ data: workflow });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(["workflow", workflow.workflow_permanent_id], workflow);
  let resolveRefetch!: (value: WorkflowApiResponse) => void;
  const refetch = vi.fn(
    () =>
      new Promise<WorkflowApiResponse>((resolve) => {
        resolveRefetch = resolve;
      }),
  );
  function CachedWorkspace() {
    const { data = workflow } = useQuery({
      queryKey: ["workflow", workflow.workflow_permanent_id],
      queryFn: refetch,
      staleTime: Infinity,
    });
    useHydrateWorkflowParameters(data, workflow.workflow_permanent_id);
    const elements = getElements(
      data.workflow_definition.blocks,
      apiWorkflowToSettings(data),
      true,
    );
    return (
      <Workspace
        initialNodes={elements.nodes}
        initialEdges={elements.edges}
        initialTitle={data.title}
        workflow={data}
        showBrowser={false}
      />
    );
  }
  const editor = () =>
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/edit"
              element={
                <ReactFlowProvider>
                  <DebugStoreContext.Provider
                    value={{ isDebugMode: false, blockRunsEnabled: false }}
                  >
                    <CachedWorkspace />
                  </DebugStoreContext.Provider>
                </ReactFlowProvider>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  let view = editor();
  const mutation = renderHook(() => useWorkflowSave(), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  try {
    act(() => {
      const saved = getElements(
        savedWorkflow.workflow_definition.blocks,
        apiWorkflowToSettings(savedWorkflow),
        true,
      );
      canvas.setNodes(saved.nodes);
      canvas.setEdges(saved.edges);
      useWorkflowTitleStore.getState().setTitle(savedWorkflow.title);
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });
    let saving!: Promise<unknown>;
    act(() => {
      saving = mutation.result.current.mutateAsync(undefined);
    });
    await waitFor(() => expect(put).toHaveBeenCalledOnce());
    const firstOwner = useWorkflowYamlEditorStore.getState().editorOwner!;
    navBlocker.state = "blocked";
    act(() =>
      useWorkflowHasChangesStore.setState({ saidOkToCodeCacheDeletion: true }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Continue without saving" }),
    );
    expect(navBlocker.proceed).toHaveBeenCalled();
    view.unmount();
    mutation.unmount();
    expect(firstOwner.active).toBe(false);
    navBlocker.state = "unblocked";
    view = editor();
    expect(useWorkflowYamlEditorStore.getState().editorOwner).not.toBe(
      firstOwner,
    );
    expect(getWorkflowBlocks(canvas.nodes, canvas.edges)).toEqual([]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    await act(async () => {
      resolveSave({ data: savedWorkflow });
      await saving;
    });
    expect(getWorkflowBlocks(canvas.nodes, canvas.edges)).toMatchObject([
      { block_type: "wait", label: "saved_wait", wait_sec: 9 },
    ]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: savedWorkflow.title,
      description: savedWorkflow.description,
    });
    expect(getWorkflowSettings(canvas.nodes).webhookCallbackUrl).toBe(
      savedWorkflow.webhook_callback_url,
    );
    expect(useWorkflowParametersStore.getState().parameters).toMatchObject([
      {
        parameterType: "context",
        key: "saved_context",
        sourceParameterKey: "input",
      },
    ]);
    expect(
      client.getQueryData(["workflow", workflow.workflow_permanent_id]),
    ).toEqual(savedWorkflow);
    expect(
      buildWorkflowSaveRequest(
        useWorkflowHasChangesStore.getState().getSaveData()!,
      ).workflow_definition.blocks,
    ).toMatchObject([{ block_type: "wait", label: "saved_wait", wait_sec: 9 }]);
    expect(refetch).toHaveBeenCalledOnce();
    await act(async () => resolveRefetch(savedWorkflow));
    expect(getWorkflowBlocks(canvas.nodes, canvas.edges)).toMatchObject([
      { block_type: "wait", label: "saved_wait", wait_sec: 9 },
    ]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  } finally {
    view.unmount();
    mutation.unmount();
    client.clear();
    clientSpy.mockRestore();
    saveHook.mockRestore();
    renderer.mockRestore();
    header.mockRestore();
    chat.mockRestore();
    navBlocker.state = "unblocked";
    vi.unstubAllGlobals();
  }
}, 20_000);

test.each([
  "edited YAML",
  "renamed YAML",
  "unchanged YAML",
  "visual",
  "visual after layout",
  "visual after dimensions",
  "visual after selection",
  "visual after delayed highlight",
  "A47 slow visual",
  "A47 slow YAML",
])(
  "hydrates normalized save settings and stays clean after saving %s",
  async (mode) => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    const workflow = {
      ...(liveWorkflow as WorkflowApiResponse),
      workflow_id: "wf_normalized",
      title: "Normalization test",
      description: null,
      webhook_callback_url: "example.com/hook",
      workflow_definition: { version: 2, blocks: [], parameters: [] },
    } as WorkflowApiResponse;
    const savedWorkflow = {
      ...workflow,
      title: "Saved title",
      webhook_callback_url: "https://example.com/hook",
      totp_verification_url: "https://example.com/totp",
      cache_key: "saved-cache",
    };
    const cosmeticChange = mode.startsWith("visual after");
    const slowSave = mode.startsWith("A47 slow");
    let finishSave:
      | ((response: { data: WorkflowApiResponse }) => void)
      | undefined;
    const put = vi.fn().mockResolvedValue({ data: savedWorkflow });
    if (cosmeticChange || slowSave) {
      put.mockImplementation(
        () =>
          new Promise((resolve) => {
            finishSave = resolve;
          }),
      );
    }
    const post = vi.fn().mockResolvedValue({
      data: { workflow_definition: workflow.workflow_definition },
    });
    const clientSpy = vi
      .spyOn(axiosClientModule, "getClient")
      .mockResolvedValue({ put, post } as never);
    const { useWorkflowSave } = await vi.importActual<
      typeof import("@/store/WorkflowHasChangesStore")
    >("@/store/WorkflowHasChangesStore");
    const saveHook = vi
      .spyOn(workflowHasChangesModule, "useWorkflowSave")
      .mockImplementation(useWorkflowSave);
    let canvas!: FlowRendererProps;
    const renderer = vi
      .spyOn(flowRendererModule, "FlowRenderer")
      .mockImplementation(function ReadyRenderer(props: FlowRendererProps) {
        canvas = props;
        const { onLayoutPhaseChange } = props;
        useEffect(() => onLayoutPhaseChange?.("ready"), [onLayoutPhaseChange]);
        return <></>;
      });
    const header = vi
      .spyOn(workflowHeaderModule, "WorkflowHeader")
      .mockReturnValue(null);
    const chat = vi
      .spyOn(copilotChatModule, "WorkflowCopilotChat")
      .mockReturnValue(null);
    workflowQueryMock.mockReturnValue({ data: workflow });
    runQueryMock.mockReturnValue({ data: undefined });
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const elements = getElements([], apiWorkflowToSettings(workflow), true);
    function SaveButton() {
      const save = useWorkflowSave();
      return (
        <button onClick={() => void save.mutateAsync(undefined)}>
          Persist visual
        </button>
      );
    }
    const view = render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/edit"
              element={
                <ReactFlowProvider>
                  <Workspace
                    initialNodes={elements.nodes}
                    initialEdges={elements.edges}
                    initialTitle={workflow.title}
                    workflow={workflow}
                    showBrowser={false}
                  />
                  <SaveButton />
                </ReactFlowProvider>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const readSaveData = () => ({
      workflow,
      title: useWorkflowTitleStore.getState().title,
      description: useWorkflowTitleStore.getState().description,
      settings: getWorkflowSettings(canvas.nodes),
      parameters: [],
      blocks: [],
      workflowDefinitionVersion: 2,
    });
    try {
      act(() => {
        useWorkflowHasChangesStore.getState().setGetSaveData(readSaveData);
        useWorkflowHasChangesStore.getState().setHasChanges(true);
      });
      if (slowSave) {
        if (mode === "A47 slow YAML") {
          act(() => {
            useWorkflowYamlEditorStore.getState().enterYamlMode!();
            const document = parse(useWorkflowYamlEditorStore.getState().draft);
            document.title = "Edited while in YAML";
            useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
          });
        }
        vi.useFakeTimers();
        let saving: Promise<boolean> | undefined;
        await act(async () => {
          if (mode === "A47 slow YAML") saving = commitYamlDraft(true);
          else screen.getByText("Persist visual").click();
        });
        await act(async () => vi.advanceTimersByTimeAsync(30_000));
        expect(screen.getByRole("status").textContent).toContain(
          "Still saving this workflow",
        );
        expect(
          screen
            .getByRole("button", { name: "Reload" })
            .getAttribute("disabled"),
        ).toBeNull();
        expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
          true,
        );
        await act(async () => {
          finishSave!({ data: savedWorkflow });
          if (saving) expect(await saving).toBe(true);
        });
        expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
        vi.useRealTimers();
      } else if (mode.startsWith("visual")) {
        const hydrateSavedSettings = vi.fn(
          useWorkflowHasChangesStore.getState().hydrateSavedSettings!,
        );
        act(() =>
          useWorkflowHasChangesStore.setState({ hydrateSavedSettings }),
        );
        await act(async () => screen.getByText("Persist visual").click());
        if (cosmeticChange) {
          await waitFor(() => expect(finishSave).toBeDefined());
          const revision = useWorkflowYamlEditorStore.getState().revision;
          act(() => {
            const { nodes, setNodes } = canvas;
            setNodes(
              nodes.map((node) => ({
                ...node,
                ...(mode === "visual after layout"
                  ? { position: { x: 50, y: 100 } }
                  : {}),
                ...(mode === "visual after dimensions"
                  ? { measured: { width: 400, height: 300 } }
                  : {}),
                ...(mode === "visual after selection"
                  ? { selected: true }
                  : {}),
                ...(mode === "visual after delayed highlight"
                  ? { className: "skyvern-block-highlight" }
                  : {}),
              })),
            );
          });
          act(() => {
            useWorkflowHasChangesStore
              .getState()
              .setGetSaveData(() => readSaveData());
          });
          expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
          expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
          expect(hydrateSavedSettings).not.toHaveBeenCalled();
          await act(async () => finishSave!({ data: savedWorkflow }));
        }
        await waitFor(() =>
          expect(hydrateSavedSettings).toHaveBeenCalledExactlyOnceWith(
            savedWorkflow,
          ),
        );
      } else {
        act(() => useWorkflowYamlEditorStore.getState().enterYamlMode!());
        if (mode === "renamed YAML") {
          act(() => {
            const document = parse(useWorkflowYamlEditorStore.getState().draft);
            document.mask_secrets = true;
            useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
            useWorkflowTitleStore.getState().setTitle("Renamed in title bar");
          });
          await act(async () =>
            expect(await commitYamlDraft(false)).toBe(false),
          );
          expect(useWorkflowTitleStore.getState().title).toBe(
            "Renamed in title bar",
          );
          expect(useWorkflowYamlEditorStore.getState().stale).toBe(true);
          act(() => {
            useWorkflowYamlEditorStore.getState().close();
            useWorkflowYamlEditorStore.getState().enterYamlMode!();
            const document = parse(useWorkflowYamlEditorStore.getState().draft);
            document.title = "Authored in YAML";
            useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
          });
          await act(async () =>
            expect(await commitYamlDraft(false)).toBe(true),
          );
          expect(useWorkflowTitleStore.getState().title).toBe(
            "Authored in YAML",
          );
          expect(useWorkflowYamlEditorStore.getState().stale).toBe(false);
          return;
        }
        if (mode === "edited YAML") {
          act(() => {
            const document = parse(useWorkflowYamlEditorStore.getState().draft);
            document.title = "Edited title";
            useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
          });
        }
        await act(async () => expect(await commitYamlDraft(true)).toBe(true));
      }
      await waitFor(() =>
        expect(readSaveData().settings.webhookCallbackUrl).toBe(
          savedWorkflow.webhook_callback_url,
        ),
      );
      expect(readSaveData().settings).toMatchObject({
        totpVerificationUrl: savedWorkflow.totp_verification_url,
        scriptCacheKey: savedWorkflow.cache_key,
      });
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      act(() => useWorkflowYamlEditorStore.getState().enterYamlMode!());
      expect(parse(useWorkflowYamlEditorStore.getState().draft)).toMatchObject({
        title: savedWorkflow.title,
        webhook_callback_url: savedWorkflow.webhook_callback_url,
        totp_verification_url: savedWorkflow.totp_verification_url,
        cache_key: savedWorkflow.cache_key,
      });
      expect(
        buildWorkflowYamlDocument({ ...readSaveData(), definitionVersion: 2 })
          .webhook_callback_url,
      ).toBe(savedWorkflow.webhook_callback_url);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    } finally {
      vi.useRealTimers();
      view.unmount();
      client.clear();
      clientSpy.mockRestore();
      saveHook.mockRestore();
      renderer.mockRestore();
      header.mockRestore();
      chat.mockRestore();
      vi.unstubAllGlobals();
    }
  },
);

describe("A49 metadata authorship through editor actions", () => {
  const workflow = {
    ...(liveWorkflow as WorkflowApiResponse),
    workflow_id: "wf_authorship",
    title: "Original title",
    description: "A",
    version: 1,
    workflow_definition: { version: 2, blocks: [], parameters: [] },
  } as WorkflowApiResponse;
  const proposed = {
    ...workflow,
    workflow_id: "wf_proposal",
    title: "Proposal title",
    description: "B",
    version: 2,
  };
  let history: WorkflowCopilotChatHistoryResponse;
  let client: QueryClient;

  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    HTMLElement.prototype.scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollTo = vi.fn();
    integration.chat = true;
    integration.canvas = false;
    integration.realNavigation = true;
    sessionStorage.clear();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowParametersStore.setState(
      useWorkflowParametersStore.getInitialState(),
    );
    useWorkflowSnapshotStore.setState(
      useWorkflowSnapshotStore.getInitialState(),
    );
    history = {
      workflow_copilot_chat_id: "chat-authorship",
      chat_history: [],
      proposed_workflow: proposed,
      proposed_workflow_metadata: {
        owner_turn_id: "turn-proposal",
        revision: 1,
        canonical_fingerprint: "fingerprint",
        disposition: "review_untested",
      },
      proposed_claim_expires_in_seconds: null,
      auto_accept: false,
    };
    historyGet
      .mockReset()
      .mockImplementation(() => Promise.resolve({ data: history }));
    post.mockReset().mockImplementation((path: string) =>
      Promise.resolve({
        data: path.endsWith("convert-yaml-to-blocks")
          ? { workflow_definition: workflow.workflow_definition }
          : proposed,
      }),
    );
    streams.length = 0;
    postStreaming.mockReset().mockImplementation(
      (_path, _body, onMessage) =>
        new Promise<void>((resolve) => {
          streams.push({ onMessage, resolve });
        }),
    );
    workflowQueryMock.mockReturnValue({ data: workflow });
    runQueryMock.mockReturnValue({ data: undefined });
    client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
  });

  afterEach(() => {
    cleanup();
    client.clear();
    integration.chat = false;
    integration.canvas = false;
  });

  function SaveButton() {
    const save = useWorkflowSave();
    return <button onClick={() => save.mutate(undefined)}>Save visual</button>;
  }

  function mountEditor(loaded = workflow) {
    const elements = getElements(
      loaded.workflow_definition.blocks,
      apiWorkflowToSettings(loaded),
      true,
    );
    const router = createMemoryRouter(
      [
        {
          path: "/agents/:workflowPermanentId/edit",
          element: (
            <DebugStoreProvider>
              <ReactFlowProvider>
                <TitleSection />
                <Link to="/away">Leave editor</Link>
                <Workspace
                  initialNodes={elements.nodes}
                  initialEdges={elements.edges}
                  initialTitle={loaded.title}
                  workflow={loaded}
                  showBrowser={false}
                />
                <SaveButton />
              </ReactFlowProvider>
            </DebugStoreProvider>
          ),
        },
        {
          path: "/away",
          element: <Link to="/agents/wpid_live/edit">Return to editor</Link>,
        },
      ],
      { initialEntries: ["/agents/wpid_live/edit"] },
    );
    const view = render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    if (!integration.canvas) {
      act(() =>
        useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
          workflow: canvas.current!.workflow,
          title: useWorkflowTitleStore.getState().title,
          description: useWorkflowTitleStore.getState().description,
          settings: getWorkflowSettings(canvas.current!.nodes),
          blocks: getWorkflowBlocks(
            canvas.current!.nodes,
            canvas.current!.edges,
          ),
          parameters: [],
          workflowDefinitionVersion: 2,
        })),
      );
    }
    return view;
  }

  function mountRecoveryEditor(loaded: WorkflowApiResponse) {
    integration.studio = true;
    integration.canvas = true;
    workflowQueryMock.mockReturnValue({ data: loaded });
    let refresh!: () => void;
    function EditorRoute() {
      const [, rerender] = useReducer((value: number) => value + 1, 0);
      refresh = rerender;
      return (
        <DebugStoreProvider>
          <WorkflowEditor />
          <SaveButton />
        </DebugStoreProvider>
      );
    }
    const router = createMemoryRouter(
      [
        {
          path: "/agents/:workflowPermanentId/studio",
          element: <EditorRoute />,
        },
      ],
      { initialEntries: ["/agents/wpid_live/studio?panes=editor,copilot"] },
    );
    const tree = () => (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    );
    const view = render(tree());
    return {
      ...view,
      refetch: (next: WorkflowApiResponse) => {
        workflowQueryMock.mockReturnValue({ data: next });
        act(() => refresh());
      },
    };
  }

  const hydrationWorkflow = {
    ...workflow,
    description: "Canonical description",
    workflow_definition: {
      ...workflow.workflow_definition,
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "string",
          key: "input",
          description: "Canonical input",
          default_value: "Canonical value",
        },
      ],
    },
  } as WorkflowApiResponse;

  function expectHydrated(loaded: WorkflowApiResponse) {
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: loaded.title,
      description: loaded.description,
    });
    expect(useWorkflowParametersStore.getState().parameters).toEqual(
      getInitialParameters(loaded),
    );
  }

  test.each(["discard", "discard after delayed PUT", "newer version"])(
    "A54 restored save hydrates before release through %s and preserves later edits",
    async (release) => {
      integration.chat = false;
      sessionStorage.setItem(
        "workflow-pending-save:wpid_live",
        JSON.stringify({
          workflowPermanentId: "wpid_live",
          baseVersion: 1,
          timestamp: Date.now(),
        }),
      );
      const view = mountRecoveryEditor(hydrationWorkflow);
      expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(true);
      const canonical =
        release === "discard"
          ? hydrationWorkflow
          : {
              ...hydrationWorkflow,
              version: 2,
              title: "New canonical title",
              description: "New canonical description",
              workflow_definition: {
                ...hydrationWorkflow.workflow_definition,
                parameters: [
                  {
                    ...hydrationWorkflow.workflow_definition.parameters[0]!,
                    key: "new_input",
                  },
                ],
              },
            };
      const dirtyRefresh = vi.spyOn(
        useWorkflowSnapshotStore.getState(),
        "noteDraftChange",
      );
      const unlocked = vi.fn(() => ({
        title: useWorkflowTitleStore.getState().title,
        description: useWorkflowTitleStore.getState().description,
        parameters: useWorkflowParametersStore.getState().parameters,
      }));
      const unsubscribe = useWorkflowYamlEditorStore.subscribe(
        (state, previous) => {
          if (previous.commitInProgress && !state.commitInProgress) unlocked();
        },
      );
      try {
        if (release.startsWith("discard")) {
          let finishRead!: (response: { data: WorkflowApiResponse }) => void;
          historyGet.mockImplementation((path: string) =>
            path === "/workflows/wpid_live"
              ? new Promise((resolve) => {
                  finishRead = resolve;
                })
              : Promise.resolve({ data: history }),
          );
          const discard = screen.getByRole("button", {
            name: "Discard pending save",
          });
          fireEvent.pointerDown(discard);
          fireEvent.click(discard);
          fireEvent.pointerUp(document);
          expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
            true,
          );
          expect(
            sessionStorage.getItem("workflow-pending-save:wpid_live"),
          ).not.toBeNull();
          await waitFor(() =>
            expect(historyGet).toHaveBeenCalledWith("/workflows/wpid_live"),
          );
          await act(async () => finishRead({ data: canonical }));
        } else {
          view.refetch(canonical);
        }
        await waitFor(() =>
          expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
            false,
          ),
        );
        expect(unlocked).toHaveBeenCalledOnce();
        expect(unlocked.mock.results[0]!.value).toEqual({
          title: canonical.title,
          description: canonical.description,
          parameters: getInitialParameters(canonical),
        });
        expectHydrated(canonical);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
        if (release === "discard")
          await waitFor(() => expect(dirtyRefresh).toHaveBeenCalledWith(true));
        expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
        put.mockReset().mockResolvedValue({ data: canonical });
        fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
        await waitFor(() => expect(put).toHaveBeenCalledOnce());
        expect(parse(put.mock.calls[0]![1])).toMatchObject({
          title: canonical.title,
          description: canonical.description,
          workflow_definition: {
            parameters: canonical.workflow_definition.parameters,
          },
        });
        await waitFor(() =>
          expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
            false,
          ),
        );
        act(() => {
          useWorkflowTitleStore.getState().setTitle("Local title");
          useWorkflowTitleStore
            .getState()
            .setDescriptionFromUser("Local description");
          useWorkflowParametersStore.getState().setParametersFromUser([]);
        });
        view.refetch({
          ...canonical,
          title: "Refetched title",
          description: "Refetched description",
        });
        expect(useWorkflowTitleStore.getState()).toMatchObject({
          title: "Local title",
          description: "Local description",
        });
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
      } finally {
        unsubscribe();
        dirtyRefresh.mockRestore();
      }
    },
  );

  test.each([200, 422])(
    "A54 remounted save with unavailable marker storage hydrates before HTTP %s releases the hold",
    async (status) => {
      integration.chat = false;
      const setItem = Storage.prototype.setItem;
      const storage = vi
        .spyOn(Storage.prototype, "setItem")
        .mockImplementation(function (
          this: Storage,
          key: string,
          value: string,
        ) {
          if (this === sessionStorage) throw new Error("Storage unavailable");
          setItem.call(this, key, value);
        });
      let settle!: () => void;
      put.mockReset().mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            settle = () =>
              status === 200
                ? resolve({ data: hydrationWorkflow })
                : reject(
                    new AxiosError(
                      "Rejected",
                      undefined,
                      undefined,
                      undefined,
                      { status, data: { detail: "Rejected" } } as NonNullable<
                        AxiosError["response"]
                      >,
                    ),
                  );
          }),
      );
      try {
        const initial = mountRecoveryEditor(hydrationWorkflow);
        fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
        await waitFor(() => expect(put).toHaveBeenCalledOnce());
        initial.unmount();
        useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
        useWorkflowParametersStore.setState(
          useWorkflowParametersStore.getInitialState(),
        );
        const returning = mountRecoveryEditor(hydrationWorkflow);
        expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
          true,
        );
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
        const unlocked = vi.fn();
        const unsubscribe = useWorkflowYamlEditorStore.subscribe(
          (state, previous) => {
            if (previous.commitInProgress && !state.commitInProgress)
              unlocked({
                title: useWorkflowTitleStore.getState().title,
                description: useWorkflowTitleStore.getState().description,
                parameters: useWorkflowParametersStore.getState().parameters,
              });
          },
        );
        try {
          await act(async () => settle());
          await waitFor(() =>
            expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(
              false,
            ),
          );
          expect(unlocked).toHaveBeenCalledExactlyOnceWith({
            title: hydrationWorkflow.title,
            description: hydrationWorkflow.description,
            parameters: getInitialParameters(hydrationWorkflow),
          });
        } finally {
          unsubscribe();
        }
        expectHydrated(hydrationWorkflow);
        act(() =>
          useWorkflowParametersStore.getState().setParametersFromUser([]),
        );
        returning.refetch({ ...hydrationWorkflow });
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
      } finally {
        storage.mockRestore();
      }
    },
  );

  test.each(["clean", "Copilot startup", "storage unavailable"])(
    "A54 %s load hydrates exactly once and keeps local edits on refetch",
    async (mode) => {
      integration.chat = mode === "Copilot startup";
      history.proposed_workflow = null;
      history.proposed_workflow_metadata = null;
      let finishHistory!: (response: {
        data: WorkflowCopilotChatHistoryResponse;
      }) => void;
      if (integration.chat)
        historyGet.mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              finishHistory = resolve;
            }),
        );
      const storage =
        mode === "storage unavailable"
          ? vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
              throw new Error("Storage unavailable");
            })
          : null;
      const parameters = vi.spyOn(
        useWorkflowParametersStore.getState(),
        "setParameters",
      );
      const titleWrites = vi.fn();
      const unsubscribe = useWorkflowTitleStore.subscribe((state, previous) => {
        if (
          state.title !== previous.title ||
          state.description !== previous.description ||
          state.titleWorkflowPermanentId !==
            previous.titleWorkflowPermanentId ||
          state.descriptionWorkflowPermanentId !==
            previous.descriptionWorkflowPermanentId
        )
          titleWrites();
      });
      try {
        const view = mountRecoveryEditor(hydrationWorkflow);
        expectHydrated(hydrationWorkflow);
        const metadataWrites = titleWrites.mock.calls.length;
        expect(parameters).toHaveBeenCalledOnce();
        if (integration.chat) {
          await waitFor(() => expect(finishHistory).toBeDefined());
          expect(
            useWorkflowHasChangesStore.getState().saveBlockedReason,
          ).toBeTruthy();
          await act(async () => finishHistory({ data: history }));
          await waitFor(() =>
            expect(
              useWorkflowHasChangesStore.getState().saveBlockedReason,
            ).toBeNull(),
          );
        }
        view.refetch({ ...hydrationWorkflow });
        expect(parameters).toHaveBeenCalledOnce();
        expect(titleWrites).toHaveBeenCalledTimes(metadataWrites);
        act(() =>
          useWorkflowParametersStore.getState().setParametersFromUser([]),
        );
        view.refetch({ ...hydrationWorkflow });
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
      } finally {
        unsubscribe();
        parameters.mockRestore();
        storage?.mockRestore();
      }
    },
  );

  test("saves YAML edits to hidden aws_secret parameters after switching to Visual and resets from canonical", async () => {
    integration.canvas = true;
    integration.chat = false;
    const original = {
      parameter_type: "aws_secret" as const,
      key: "original_reference",
      description: "Reference description",
      aws_key: "original_key",
    };
    const edited = {
      ...original,
      key: "edited_reference",
      aws_key: "edited_key",
    };
    const loaded = {
      ...workflow,
      workflow_definition: {
        ...workflow.workflow_definition,
        parameters: [original],
      },
    } as WorkflowApiResponse;
    const canonicalParameter = { ...edited, key: "canonical_reference" };
    const canonical = {
      ...loaded,
      version: 2,
      workflow_definition: {
        ...loaded.workflow_definition,
        parameters: [canonicalParameter],
      },
    } as WorkflowApiResponse;
    post.mockImplementation((_path, body) =>
      Promise.resolve({
        data: {
          workflow_definition: parse(body.workflow_definition_yaml),
        },
      }),
    );
    put.mockReset().mockResolvedValue({ data: canonical });
    mountEditor(loaded);
    act(() => {
      useWorkflowYamlEditorStore.getState().enterYamlMode!();
      const document = parse(useWorkflowYamlEditorStore.getState().draft);
      document.workflow_definition.parameters = [edited];
      useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
    });
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    await waitFor(() =>
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
    );
    expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
    await waitFor(() => expect(put).toHaveBeenCalledOnce());
    expect(parse(put.mock.calls[0]![1]).workflow_definition.parameters).toEqual(
      [edited],
    );
    await waitFor(() =>
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false),
    );
    act(() => useWorkflowYamlEditorStore.getState().enterYamlMode!());
    expect(
      parse(useWorkflowYamlEditorStore.getState().draft).workflow_definition
        .parameters,
    ).toEqual([canonicalParameter]);
    act(() => {
      const document = parse(useWorkflowYamlEditorStore.getState().draft);
      document.workflow_definition.parameters = [];
      useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
    });
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    await waitFor(() =>
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(parse(put.mock.calls[1]![1]).workflow_definition.parameters).toEqual(
      [],
    );
  });

  test("keeps generated output parameters out of YAML and the visual PUT after a block rename", async () => {
    integration.canvas = true;
    integration.chat = false;
    const loaded = {
      ...workflow,
      workflow_definition: {
        version: 2,
        blocks: [{ block_type: "wait", label: "pause", wait_sec: 5 }],
        parameters: [
          { parameter_type: "output", key: "pause_output", description: null },
        ],
      },
    } as WorkflowApiResponse;
    const converted = {
      ...loaded.workflow_definition,
      blocks: [{ block_type: "wait", label: "renamed", wait_sec: 5 }],
      parameters: [
        { parameter_type: "output", key: "renamed_output", description: null },
      ],
    };
    post.mockResolvedValue({ data: { workflow_definition: converted } });
    put.mockReset();
    put.mockResolvedValue({
      data: { ...loaded, version: 2, workflow_definition: converted },
    });
    mountEditor(loaded);
    act(() => useWorkflowYamlEditorStore.getState().enterYamlMode!());
    const document = parse(useWorkflowYamlEditorStore.getState().draft);
    expect(document.workflow_definition.parameters).toEqual([]);
    document.workflow_definition.blocks[0].label = "renamed";
    act(() =>
      useWorkflowYamlEditorStore.getState().setDraft(stringify(document)),
    );
    fireEvent.click(screen.getByRole("button", { name: "Visual" }));
    await waitFor(() =>
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
    );
    expect(
      parse(post.mock.calls[0]![1].workflow_definition_yaml).parameters,
    ).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
    await waitFor(() => expect(put).toHaveBeenCalledOnce());
    const request = parse(put.mock.calls[0]![1]);
    expect(request.workflow_definition.parameters).toEqual([]);
    expect(request.workflow_definition.blocks[0]).toMatchObject({
      label: "renamed",
    });
  });

  function rename(title: string) {
    fireEvent.click(
      screen.getByRole("button", { name: "Click to edit title" }),
    );
    const input = document.activeElement as HTMLInputElement;
    fireEvent.change(input, { target: { value: title } });
    fireEvent.blur(input);
  }

  async function send(message: string) {
    const input = screen
      .getAllByRole("textbox")
      .find((element) => element.tagName === "TEXTAREA")!;
    fireEvent.change(input, { target: { value: message } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(streams).toHaveLength(1));
  }

  async function accept() {
    const button = await screen.findByRole("button", { name: "Accept" });
    await act(async () => fireEvent.click(button));
    await waitFor(() =>
      expect(
        post.mock.calls.some(([path]) =>
          path.endsWith("apply-proposed-workflow"),
        ),
      ).toBe(true),
    );
  }

  function stageGraphProposal() {
    const staged = {
      ...proposed,
      workflow_definition: {
        version: 2,
        blocks: [
          {
            block_type: "wait",
            label: "pause",
            wait_sec: 5,
            output_parameter: {
              key: "pause_output",
              description: null,
              parameter_type: "output",
              output_parameter_id: "op_pause",
              workflow_id: "wf_proposal",
              created_at: "",
              modified_at: "",
              deleted_at: null,
            },
            continue_on_failure: false,
            model: null,
          },
        ],
        parameters: [],
      },
    } as WorkflowApiResponse;
    history.proposed_workflow = staged;
    post.mockResolvedValue({ data: staged });
    return staged;
  }

  function editStagedBlock() {
    if (integration.canvas) {
      act(() =>
        useWorkflowPanelStore
          .getState()
          .setSelectedBlockId(
            canvas.current!.nodes.find((node) => node.type === "wait")!.id,
          ),
      );
      fireEvent.change(screen.getAllByDisplayValue("5")[0]!, {
        target: { value: "17" },
      });
      act(() => useWorkflowHasChangesStore.getState().setHasChanges(true));
      return;
    }
    act(() => {
      canvas.current!.setNodes(
        canvas.current!.nodes.map((node) =>
          node.type === "wait"
            ? { ...node, data: { ...node.data, waitInSeconds: "17" } }
            : node,
        ),
      );
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });
  }

  test.each(
    [false, true].flatMap((recover) =>
      ["Keep my edits", "Apply and discard my edits"].map((choice) => ({
        recover,
        choice,
      })),
    ),
  )(
    "A53 model input after a proposal uses $choice (recovery: $recover)",
    async ({ recover, choice }) => {
      integration.canvas = true;
      const staged = { ...proposed, model: { model_name: "proposal-model" } };
      history.proposed_workflow = staged;
      post.mockResolvedValue({ data: staged });
      client.setQueryData(["models"], {
        models: {
          "proposal-model": "Proposal model",
          "user-model": "User model",
        },
      });
      mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
      await screen.findByRole("button", { name: "Accept" });
      act(() =>
        useWorkflowPanelStore
          .getState()
          .setSelectedBlockId(
            canvas.current!.nodes.find((node) => node.type === "start")!.id,
          ),
      );
      const selector = screen
        .getAllByRole("combobox")
        .find((element) => element.textContent?.includes("Proposal model"))!;
      fireEvent.keyDown(selector, { key: " " });
      fireEvent.click(
        await screen.findByRole("option", { name: "User model" }),
      );
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings.model,
      ).toEqual({ model_name: "user-model" });
      if (recover) {
        historyGet.mockImplementation((path: string) =>
          Promise.resolve({
            data: path.startsWith("/workflows/")
              ? staged
              : {
                  ...history,
                  proposed_workflow: null,
                  proposed_workflow_metadata: null,
                },
          }),
        );
        post.mockRejectedValue(new Error("Lost response"));
      }
      await accept();
      fireEvent.click(await screen.findByRole("button", { name: choice }));
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(saved.settings.model).toEqual({
        model_name:
          choice === "Keep my edits" ? "user-model" : "proposal-model",
      });
      expect(saved.workflow.workflow_id).toBe(staged.workflow_id);
      expect(saved.workflow.version).toBe(2);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        choice === "Keep my edits",
      );
      if (choice === "Keep my edits") {
        expect(
          useWorkflowSnapshotStore.getState().snapshot?.settings.model,
        ).toEqual(staged.model);
      }
    },
  );

  test.each(["recording", "SOP conversion"])(
    "keeps generated blocks unsaved after accepting a proposal following %s",
    async (source) => {
      integration.canvas = true;
      const staged = stageGraphProposal();
      mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
      await screen.findByRole("button", { name: "Accept" });
      const generated = {
        blocks: [
          { ...staged.workflow_definition.blocks[0]!, label: "generated" },
        ],
        parameters: [],
      };
      act(() => {
        const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
        if (source === "SOP conversion") {
          applySopResultAtCurrentAppend({
            result: generated,
            getNodes: () => canvas.current!.nodes,
            getEdges: () => canvas.current!.edges,
            setRecordedBlocks: (result, insertionPoint) =>
              useRecordedBlocksStore
                .getState()
                .setRecordedBlocks(result, insertionPoint, owner),
          });
        } else {
          useRecordedBlocksStore
            .getState()
            .setRecordedBlocks(
              generated,
              { previous: null, next: null, connectingEdgeType: "default" },
              owner,
            );
        }
      });
      await waitFor(() =>
        expect(
          useWorkflowHasChangesStore
            .getState()
            .getSaveData()!
            .blocks.map((block) => block.label),
        ).toEqual(["pause", "generated"]),
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      await accept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Keep my edits" }),
      );
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(saved.blocks.map((block) => block.label)).toEqual([
        "pause",
        "generated",
      ]);
      expect(saved.workflow.workflow_id).toBe(staged.workflow_id);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    },
  );

  test.each([
    { label: "Disable parallel runs", setting: "runSequentially" as const },
    {
      label: "Reuse browser sessions",
      setting: "reuseBrowserSession" as const,
    },
  ])(
    "keeps the login selector's $label edit dirty after accepting a proposal",
    async ({ label, setting }) => {
      integration.canvas = true;
      const credentials = ["cred_primary", "cred_secondary"].map((id) => ({
        credential_id: id,
        name: id,
        credential_type: "password",
        credential: {},
      }));
      const parameter: CredentialParameter = {
        parameter_type: "credential",
        key: "credentials",
        description: null,
        workflow_id: proposed.workflow_id,
        credential_parameter_id: "cp_rotation",
        created_at: "",
        modified_at: "",
        deleted_at: null,
        credential_id: "cred_primary",
        credential_ids: ["cred_primary", "cred_secondary"],
        selection_strategy: "round_robin",
      };
      const staged = {
        ...proposed,
        run_sequentially: setting === "reuseBrowserSession",
        reuse_browser_session: false,
        persist_browser_session: false,
        workflow_definition: {
          version: 2,
          parameters: [parameter],
          blocks: [
            {
              block_type: "login",
              label: "login",
              url: "https://example.test/login",
              navigation_goal: "Log in",
              parameters: [{ key: parameter.key }],
              continue_on_failure: false,
              model: null,
              error_code_mapping: null,
            },
          ],
        },
      } as WorkflowApiResponse;
      history.proposed_workflow = staged;
      post.mockResolvedValue({ data: staged });
      historyGet.mockImplementation((path: string) =>
        Promise.resolve({
          data:
            path === "/credentials"
              ? credentials
              : path === "/credentials/onepassword/items"
                ? { configured: false, source: null, items: [] }
                : path.startsWith("/credentials/")
                  ? credentials.find((credential) =>
                      path.endsWith(credential.credential_id),
                    )
                  : history,
        }),
      );
      useWorkflowParametersStore.setState({
        parameters: getInitialParameters(staged),
      });
      mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
      await screen.findByRole("button", { name: "Accept" });
      expect(
        useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_live,
      ).toBeDefined();
      expect(
        useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_live
          ?.graphEdited,
      ).toBeUndefined();
      act(() =>
        useWorkflowPanelStore
          .getState()
          .setSelectedBlockId(
            canvas.current!.nodes.find((node) => node.type === "login")!.id,
          ),
      );
      const selector = within(await screen.findByTestId("login-block-form"));
      const toggleLabel = await selector.findByText(label);
      const toggle = within(
        toggleLabel.parentElement!.parentElement!,
      ).getByRole("switch");
      fireEvent.click(toggle);
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings[setting],
      ).toBe(true);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

      await accept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Keep my edits" }),
      );
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(saved.settings[setting]).toBe(true);
      expect(saved.workflow.workflow_id).toBe(staged.workflow_id);
      expect(saved.workflow.version).toBe(2);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(
        useWorkflowSnapshotStore.getState().snapshot?.settings[setting],
      ).toBe(false);
    },
  );

  test.each(["max_elapsed_time_minutes", "retry_policy"])(
    "A53 a Code-mode commit changing only %s records a proposal conflict",
    async (setting) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      const retryPolicy = {
        max_retries: 2,
        delay_seconds: 10,
        webhook_on_retry: "final_only",
        retry_on: [{ status: "failed", error_codes: ["RETRYABLE"] }],
      };
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        const document = parse(useWorkflowYamlEditorStore.getState().draft);
        if (setting === "retry_policy")
          document.workflow_definition.retry_policy = retryPolicy;
        else document.max_elapsed_time_minutes = 37;
        useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
      });
      fireEvent.click(screen.getByRole("button", { name: "Visual" }));
      await waitFor(() =>
        expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
      );
      await accept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Keep my edits" }),
      );
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      if (setting === "retry_policy")
        expect(saved.settings.retryPolicy).toEqual(retryPolicy);
      else expect(saved.settings.maxElapsedTimeMinutes).toBe(37);
      expect(saved.workflow.version).toBe(2);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    },
  );

  test("A53 automatic settings repair does not author a proposal edit", async () => {
    integration.canvas = true;
    const loaded = {
      ...workflow,
      workflow_definition: {
        ...workflow.workflow_definition,
        finally_block_label: "missing_block",
      },
    };
    mountEditor(loaded);
    await screen.findByRole("button", { name: "Accept" });
    act(() =>
      useWorkflowPanelStore
        .getState()
        .setSelectedBlockId(
          canvas.current!.nodes.find((node) => node.type === "start")!.id,
        ),
    );
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings
          .finallyBlockLabel,
      ).toBeNull(),
    );
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_live"]
        ?.graphEdited,
    ).toBeUndefined();
    await accept();
    expect(screen.queryByRole("button", { name: "Keep my edits" })).toBeNull();
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  test.each([false, true])(
    "A53 a Copilot settings draft records no edit (metadata-only YAML commit: %s)",
    async (commitMetadata) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      await send("Explain the proposal");
      await act(async () => {
        streams[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: { ...proposed, max_elapsed_time_minutes: 43 },
        });
        streams[0]!.onMessage({
          type: "response",
          workflow_copilot_chat_id: "chat-authorship",
          turn_id: "turn-followup",
          message: "Explanation only",
          updated_workflow: null,
          proposal_disposition: "no_proposal",
        });
        streams[0]!.resolve();
      });
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings
          .maxElapsedTimeMinutes,
      ).toBe(43);
      if (commitMetadata) {
        act(() => {
          useWorkflowYamlEditorStore.getState().enterYamlMode!();
          const document = parse(useWorkflowYamlEditorStore.getState().draft);
          document.title = "Authored title";
          useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
        });
        fireEvent.click(screen.getByRole("button", { name: "Visual" }));
        await waitFor(() =>
          expect(useWorkflowYamlEditorStore.getState().active).toBe(false),
        );
      }
      expect(
        useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_live"]
          ?.graphEdited,
      ).toBeUndefined();
      await accept();
      expect(
        screen.queryByRole("button", { name: "Keep my edits" }),
      ).toBeNull();
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings
          .maxElapsedTimeMinutes,
      ).toBe(proposed.max_elapsed_time_minutes ?? null);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        commitMetadata,
      );
    },
  );

  test.each(["Keep my edits", "Apply and discard my edits"])(
    "A51 Accept offers %s for a block edited after the proposal",
    async (choice) => {
      integration.canvas = true;
      const staged = stageGraphProposal();
      mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
      await screen.findByRole("button", { name: "Accept" });
      editStagedBlock();
      await accept();
      fireEvent.click(await screen.findByRole("button", { name: choice }));
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      const data = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(data.blocks[0]).toMatchObject({
        wait_sec: choice === "Keep my edits" ? 17 : 5,
      });
      expect(data.workflow.workflow_id).toBe(staged.workflow_id);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        choice === "Keep my edits",
      );
    },
  );

  test("A51 a Copilot draft does not author a graph edit", async () => {
    const staged = stageGraphProposal();
    mountEditor();
    await screen.findByRole("button", { name: "Accept" });
    await send("Explain the proposal");
    await act(async () => {
      streams[0]!.onMessage({
        type: "workflow_draft",
        block_labels: ["pause"],
        workflow: {
          ...staged,
          workflow_definition: {
            ...staged.workflow_definition,
            blocks: [{ block_type: "wait", label: "pause", wait_sec: 23 }],
          },
        },
      });
      streams[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-authorship",
        turn_id: "turn-followup",
        message: "Explanation only",
        updated_workflow: null,
        proposal_disposition: "no_proposal",
      });
      streams[0]!.resolve();
    });
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
    ).toMatchObject({ wait_sec: 23 });
    await accept();
    expect(screen.queryByRole("button", { name: "Keep my edits" })).toBeNull();
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
    ).toMatchObject({ wait_sec: 5 });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  test.each(["Keep my edits", "Apply and discard my edits"])(
    "A51 parameter edits survive Accept according to %s",
    async (choice) => {
      integration.canvas = true;
      const staged = stageGraphProposal();
      mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
      await screen.findByRole("button", { name: "Accept" });
      act(() => {
        useWorkflowParametersStore.getState().setParametersFromUser([
          {
            parameterType: "credential",
            key: "login",
            credentialId: "cred_example",
          },
        ]);
      });
      await accept();
      fireEvent.click(await screen.findByRole("button", { name: choice }));
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      expect(useWorkflowParametersStore.getState().parameters).toHaveLength(
        choice === "Keep my edits" ? 1 : 0,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        choice === "Keep my edits",
      );
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.workflow.version,
      ).toBe(2);
    },
  );

  test("A51 YAML block commits author a graph edit", async () => {
    const staged = stageGraphProposal();
    mountEditor(staged);
    await screen.findByRole("button", { name: "Accept" });
    post.mockImplementation((path: string) =>
      Promise.resolve({
        data: path.endsWith("convert-yaml-to-blocks")
          ? {
              workflow_definition: {
                ...staged.workflow_definition,
                blocks: [{ block_type: "wait", label: "pause", wait_sec: 19 }],
              },
            }
          : staged,
      }),
    );
    act(() => {
      useWorkflowYamlEditorStore.getState().enterYamlMode!();
      const document = parse(useWorkflowYamlEditorStore.getState().draft);
      document.workflow_definition.blocks[0].wait_sec = 19;
      useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
    });
    await act(async () => expect(await commitYamlDraft(false)).toBe(true));
    await accept();
    fireEvent.click(
      await screen.findByRole("button", { name: "Keep my edits" }),
    );
    await waitFor(() =>
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull(),
    );
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
    ).toMatchObject({ wait_sec: 19 });
  });

  test("A51 a recovered Accept still requires a graph choice", async () => {
    const staged = stageGraphProposal();
    mountEditor({ ...staged, workflow_id: workflow.workflow_id, version: 1 });
    await screen.findByRole("button", { name: "Accept" });
    editStagedBlock();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve({
        data: path.startsWith("/workflows/")
          ? staged
          : {
              ...history,
              proposed_workflow: null,
              proposed_workflow_metadata: null,
            },
      }),
    );
    post.mockRejectedValue(new Error("Lost response"));
    await accept();
    fireEvent.click(
      await screen.findByRole("button", { name: "Keep my edits" }),
    );
    await waitFor(() =>
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull(),
    );
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
    ).toMatchObject({ wait_sec: 17 });
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.workflow.version,
    ).toBe(2);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });

  test("A51 automatic node hydration does not author a graph edit", async () => {
    const staged = stageGraphProposal();
    mountEditor(staged);
    await screen.findByRole("button", { name: "Accept" });
    const node = canvas.current!.nodes.find((node) => node.type === "wait")!;
    act(() =>
      canvas.current!.onNodesChange([
        {
          type: "replace",
          id: node.id,
          item: { ...node, data: { ...node.data, waitInSeconds: "29" } },
        },
      ]),
    );
    await accept();
    expect(screen.queryByRole("button", { name: "Keep my edits" })).toBeNull();
    expect(
      useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
    ).toMatchObject({ wait_sec: 5 });
  });

  test.each([false, true])(
    "A51 graph authorship resets only for a new proposal: %s",
    async (newProposal) => {
      const staged = stageGraphProposal();
      mountEditor(staged);
      await screen.findByRole("button", { name: "Accept" });
      editStagedBlock();
      await send("Explain this proposal");
      await act(async () => {
        streams[0]!.onMessage({
          type: "response",
          workflow_copilot_chat_id: "chat-authorship",
          turn_id: "turn-followup",
          message: "Follow up",
          updated_workflow: newProposal ? staged : null,
          proposal_disposition: newProposal ? "review_untested" : "no_proposal",
          proposed_workflow_metadata: newProposal
            ? {
                ...history.proposed_workflow_metadata,
                owner_turn_id: "turn-followup",
                revision: 2,
              }
            : null,
        });
        streams[0]!.resolve();
      });
      await accept();
      if (newProposal) {
        expect(
          screen.queryByRole("button", { name: "Keep my edits" }),
        ).toBeNull();
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      } else {
        expect(
          await screen.findByRole("button", { name: "Keep my edits" }),
        ).toBeTruthy();
      }
    },
  );

  test("A51 a workflow refetch retains parameters kept locally", async () => {
    const tree = () => (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_live/studio"]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/studio"
              element={<WorkflowEditor />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(tree());
    act(() => {
      useWorkflowParametersStore.getState().setParametersFromUser([
        {
          parameterType: "credential",
          key: "login",
          credentialId: "cred_example",
        },
      ]);
    });
    workflowQueryMock.mockReturnValue({ data: proposed });
    view.rerender(tree());
    expect(useWorkflowParametersStore.getState().parameters).toHaveLength(1);
  });

  test("A51 leaving without saving clears graph authorship", async () => {
    integration.canvas = true;
    const staged = stageGraphProposal();
    mountEditor(staged);
    await screen.findByRole("button", { name: "Accept" });
    editStagedBlock();
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_live"],
    ).toMatchObject({ graphEdited: true });
    fireEvent.click(screen.getByRole("link", { name: "Leave editor" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Continue without saving" }),
    );
    fireEvent.click(
      await screen.findByRole("link", { name: "Return to editor" }),
    );
    await accept();
    expect(screen.queryByRole("button", { name: "Keep my edits" })).toBeNull();
    expect(
      useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_live"],
    ).not.toMatchObject({ graphEdited: true });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  test.each([false, true])(
    "YAML settings edits do not author carried descriptions (title edited: %s)",
    async (editTitle) => {
      history.proposed_workflow = null;
      history.proposed_workflow_metadata = null;
      mountEditor();
      await waitFor(() => expect(historyGet).toHaveBeenCalled());
      await act(async () => {});
      await send("Update description");
      await act(async () => {
        for (const description of ["B", "C"])
          streams[0]!.onMessage({
            type: "workflow_draft",
            block_labels: [],
            workflow: { ...proposed, description },
          });
        streams[0]!.onMessage({
          type: "response",
          workflow_copilot_chat_id: "chat-authorship",
          turn_id: "turn-proposal",
          message: "Draft ready",
          updated_workflow: proposed,
          proposal_disposition: "review_untested",
          proposed_workflow_metadata: {
            owner_turn_id: "turn-proposal",
            revision: 1,
            canonical_fingerprint: "fingerprint",
            disposition: "review_untested",
          },
        });
        streams[0]!.resolve();
      });
      expect(useWorkflowTitleStore.getState().description).toBe("C");
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        const document = parse(useWorkflowYamlEditorStore.getState().draft);
        document.max_screenshot_scrolls = 7;
        if (editTitle) document.title = "YAML rename";
        useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(true));
      expect(useWorkflowYamlEditorStore.getState().active).toBe(false);
      expect(
        useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_live"]
          ?.edits,
      ).toEqual(editTitle ? { title: "YAML rename" } : {});
      await accept();
      fireEvent.click(
        await screen.findByRole("button", { name: "Keep my edits" }),
      );
      await waitFor(() =>
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull(),
      );
      expect(useWorkflowTitleStore.getState().description).toBe("B");
      const saved = {
        workflow: proposed,
        title: proposed.title,
        description: proposed.description,
        settings: apiWorkflowToSettings(proposed),
        blocks: [],
        parameters: [],
        workflowDefinitionVersion: 2,
      };
      expect(
        summarizeWorkflowChanges(
          useWorkflowHasChangesStore.getState().getSaveData()!,
          snapshotOf(saved),
        ),
      ).not.toContain("Description");
      expect(useWorkflowTitleStore.getState().title).toBe(
        editTitle ? "YAML rename" : "Proposal title",
      );
    },
  );

  test.each([false, true])(
    "a follow-up resets the rename only for a new proposal (new proposal: %s)",
    async (newProposal) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      rename("Kept rename");
      await send("Explain this proposal");
      await act(async () => {
        streams[0]!.onMessage({
          type: "response",
          workflow_copilot_chat_id: "chat-authorship",
          turn_id: "turn-followup",
          message: newProposal ? "New draft" : "Explanation only",
          updated_workflow: newProposal ? proposed : null,
          proposal_disposition: newProposal ? "review_untested" : "no_proposal",
          proposed_workflow_metadata: newProposal
            ? {
                ...history.proposed_workflow_metadata,
                owner_turn_id: "turn-followup",
                revision: 2,
              }
            : null,
        });
        streams[0]!.resolve();
      });
      await accept();
      expect(useWorkflowTitleStore.getState().title).toBe(
        newProposal ? "Proposal title" : "Kept rename",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        !newProposal,
      );
    },
  );

  test("a rename while history loads belongs to the loaded proposal", async () => {
    let finishHistory!: (response: {
      data: WorkflowCopilotChatHistoryResponse;
    }) => void;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishHistory = resolve;
        }),
    );
    mountEditor();
    await waitFor(() => expect(finishHistory).toBeDefined());
    rename("Startup rename");
    await act(async () => finishHistory({ data: history }));
    await accept();
    expect(useWorkflowTitleStore.getState().title).toBe("Startup rename");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });

  test("leaving without saving discards the rename before returning and accepting", async () => {
    integration.canvas = true;
    mountEditor();
    await screen.findByRole("button", { name: "Accept" });
    rename("Discarded rename");
    fireEvent.click(screen.getByRole("link", { name: "Leave editor" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Continue without saving" }),
    );
    fireEvent.click(
      await screen.findByRole("link", { name: "Return to editor" }),
    );
    await accept();
    expect(useWorkflowTitleStore.getState().title).toBe("Proposal title");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  test.each([false, true])(
    "selecting a saved variant discards only earlier metadata edits (history loading: %s)",
    async (historyLoading) => {
      let finishHistory:
        | ((response: { data: WorkflowCopilotChatHistoryResponse }) => void)
        | undefined;
      if (historyLoading)
        historyGet.mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              finishHistory = resolve;
            }),
        );
      mountEditor();
      if (historyLoading)
        await waitFor(() => expect(finishHistory).toBeDefined());
      else await screen.findByRole("button", { name: "Accept" });
      rename("Discarded rename");
      act(() =>
        useWorkflowPanelStore.getState().setWorkflowPanelState({
          active: false,
          content: "history",
          data: {
            showComparison: true,
            version1: workflow,
            version2: proposed,
            mode: "history",
          },
        }),
      );
      fireEvent.click(
        screen.getAllByRole("button", { name: "Select this variant" })[0]!,
      );
      expect(useWorkflowTitleStore.getState().title).toBe(workflow.title);
      if (historyLoading) {
        rename("Rename after discarding");
        await act(async () => finishHistory!({ data: history }));
      }
      await accept();
      expect(useWorkflowTitleStore.getState().title).toBe(
        historyLoading ? "Rename after discarding" : proposed.title,
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(
        historyLoading,
      );
    },
  );

  test.each([
    ["US-CA", "US-CA"],
    ["RESIDENTIAL", "XX-ZZ"],
  ])(
    "YAML-to-Visual preserves an unlisted proxy string from %s to %s on Save",
    async (loadedProxy, proxy) => {
      const loaded = {
        ...workflow,
        proxy_location: loadedProxy as WorkflowApiResponse["proxy_location"],
      };
      put.mockReset().mockResolvedValue({
        data: { ...loaded, title: "Edited title", proxy_location: proxy },
      });
      mountEditor(loaded);
      await screen.findByRole("button", { name: "Accept" });
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        const document = parse(useWorkflowYamlEditorStore.getState().draft);
        expect(document.proxy_location).toBe(loadedProxy);
        document.title = "Edited title";
        document.proxy_location = proxy;
        useWorkflowYamlEditorStore.getState().setDraft(stringify(document));
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(true));
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: false,
        error: null,
      });
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(saved.title).toBe("Edited title");
      expect(saved.settings.proxyLocation).toBe(proxy);
      const onProxyChange = vi.fn();
      render(
        <ProxySelector
          value={saved.settings.proxyLocation}
          onChange={onProxyChange}
        />,
      );
      expect(onProxyChange).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Save visual" }));
      await waitFor(() => expect(put).toHaveBeenCalledOnce());
      expect(parse(put.mock.calls[0]![1])).toMatchObject({
        title: "Edited title",
        proxy_location: proxy,
      });
      expect(onProxyChange).not.toHaveBeenCalled();
    },
  );

  test.each(["", "not-a-url", "http://", "://missing-scheme", "http://[::1"])(
    "YAML-to-Visual rejects a custom proxy URL without a host (%j)",
    async (url) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      const before = useWorkflowHasChangesStore.getState().getSaveData()!;
      const yaml = stringify({
        proxy_location: { url },
        workflow_definition: { blocks: [] },
      });
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        useWorkflowYamlEditorStore.getState().setDraft(yaml);
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(false));
      const error = "proxy_location.url: custom proxy URL must include a host";
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: true,
        draft: yaml,
        commitInProgress: false,
        error,
      });
      expect(screen.getByText(error)).toBeTruthy();
      expect(useWorkflowHasChangesStore.getState().getSaveData()).toEqual(
        before,
      );
      expect(post).not.toHaveBeenCalled();
      expect(put).not.toHaveBeenCalled();
    },
  );

  test.each([
    "http://user:secret@proxy.example.com:8080",
    "http://proxy.example.com:8080",
    "socks5://u:p@10.0.0.1:1080",
    "http://user:p%40ss%3Aword@proxy.example.com:8080",
    "//proxy.example.com:8080",
    "http://[2001:db8::1]:3128",
    "http://[::1]:8080",
    "http://[::1]",
  ])(
    "YAML-to-Visual commits a custom proxy URL with a host and withholds it from Copilot (%s)",
    async (url) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      const proxy = { url };
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        useWorkflowYamlEditorStore.getState().setDraft(
          stringify({
            proxy_location: proxy,
            workflow_definition: { blocks: [] },
          }),
        );
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(true));
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: false,
        error: null,
      });
      const saved = useWorkflowHasChangesStore.getState().getSaveData()!;
      expect(saved.settings.proxyLocation).toEqual(proxy);
      expect(buildWorkflowSaveRequest(saved).proxy_location).toEqual(proxy);
      const context = buildWorkflowCopilotContext({
        ...saved,
        definitionVersion: saved.workflowDefinitionVersion,
      });
      expect(context.document).not.toHaveProperty("proxy_location");
      expect(context.snapshot.proxy_location).toEqual(proxy);
    },
  );

  test.each([
    ["subdivision", 10],
    ["city", 100],
  ] as const)(
    "YAML-to-Visual rejects a geo target %s above its %i-character limit",
    async (field, limit) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      const before = useWorkflowHasChangesStore.getState().getSaveData()!;
      const yaml = stringify({
        proxy_location: { country: "US", [field]: "x".repeat(limit + 1) },
        workflow_definition: { blocks: [] },
      });
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        useWorkflowYamlEditorStore.getState().setDraft(yaml);
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(false));
      const error = `proxy_location.${field}: must be at most ${limit} characters`;
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: true,
        draft: yaml,
        commitInProgress: false,
        error,
      });
      expect(screen.getByText(error)).toBeTruthy();
      expect(useWorkflowHasChangesStore.getState().getSaveData()).toEqual(
        before,
      );
      expect(post).not.toHaveBeenCalled();
    },
  );

  test.each(["x", "🌍"])(
    "YAML-to-Visual commits a geo target with isISP at the character limits (%s)",
    async (character) => {
      mountEditor();
      await screen.findByRole("button", { name: "Accept" });
      const proxy = {
        country: "US",
        subdivision: character.repeat(10),
        city: character.repeat(100),
        isISP: true,
      };
      act(() => {
        useWorkflowYamlEditorStore.getState().enterYamlMode!();
        useWorkflowYamlEditorStore.getState().setDraft(
          stringify({
            proxy_location: proxy,
            workflow_definition: { blocks: [] },
          }),
        );
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(true));
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: false,
        error: null,
      });
      expect(
        useWorkflowHasChangesStore.getState().getSaveData()!.settings
          .proxyLocation,
      ).toEqual(proxy);
    },
  );

  test("YAML-to-Visual rejects an empty retry error code and retains the draft after cleanup", async () => {
    mountEditor();
    await screen.findByRole("button", { name: "Accept" });
    const yaml =
      'workflow_definition:\n  blocks: []\n  retry_policy:\n    retry_on:\n      - status: failed\n        error_codes: [""]\n';
    act(() => {
      useWorkflowYamlEditorStore.getState().enterYamlMode!();
      useWorkflowYamlEditorStore.getState().setDraft(yaml);
    });
    await act(async () => expect(await commitYamlDraft(false)).toBe(false));
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      active: true,
      draft: yaml,
      committing: false,
      commitInProgress: false,
      commitOwner: null,
      lockKind: null,
      error: expect.stringContaining("retry_policy.rules[0].error_codes[0]"),
    });
    expect(
      screen.getByText(/retry_policy.rules\[0\].error_codes\[0\]/),
    ).toBeTruthy();
    expect(post).not.toHaveBeenCalled();
  });
});

describe("save callback registration after explicit edits", () => {
  const saveData = (): WorkflowSaveData => ({
    workflow: liveWorkflow,
    title: "Live agent",
    description: null,
    settings: apiWorkflowToSettings(liveWorkflow),
    blocks: [],
    parameters: [],
    workflowDefinitionVersion: 2,
  });

  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
  });

  test("registers identical serialized data without advancing the revision", () => {
    const previous = saveData();
    const changes = useWorkflowHasChangesStore.getState();
    const initialRevision = useWorkflowYamlEditorStore.getState().revision;
    changes.setGetSaveData(() => previous);
    const revision = useWorkflowYamlEditorStore.getState().revision;
    expect(revision).toBe(initialRevision);
    const next = () => ({
      ...saveData(),
      description: "",
      settings: {
        ...previous.settings,
        extraHttpHeaders: "{}",
        scriptCacheKey: "default",
      },
    });
    changes.setGetSaveData(next);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision);
    expect(useWorkflowHasChangesStore.getState().getSaveData).toBe(next);
  });

  test.each([
    { title: "Edited title" },
    { description: "Edited description" },
    { settings: { ...saveData().settings, maxScreenshotScrolls: 7 } },
    {
      parameters: [
        {
          parameter_type: "workflow",
          key: "input",
          workflow_parameter_type: "string",
        },
      ],
    },
    {
      blocks: [
        {
          block_type: "goto_url",
          label: "open_page",
          url: "https://example.com",
        },
      ],
    },
    { workflowDefinitionVersion: 1 },
    { workflow: { ...(liveWorkflow as WorkflowApiResponse), status: "draft" } },
  ] satisfies Array<Partial<WorkflowSaveData>>)(
    "advances the revision once for changed request data: %j",
    (patch) => {
      const previous = saveData();
      const changes = useWorkflowHasChangesStore.getState();
      changes.setGetSaveData(() => previous);
      const revision = useWorkflowYamlEditorStore.getState().revision;
      changes.setHasChanges(true);
      changes.setGetSaveData(() => ({ ...previous, ...patch }));
      expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
    },
  );

  test("keeps registration usable and advances the revision when settings cannot serialize", () => {
    const changes = useWorkflowHasChangesStore.getState();
    const previous = saveData();
    changes.setGetSaveData(() => previous);
    const invalid = () => ({
      ...previous,
      settings: { ...previous.settings, extraHttpHeaders: "{" },
    });
    let revision = useWorkflowYamlEditorStore.getState().revision;
    changes.setHasChanges(true);
    expect(() => changes.setGetSaveData(invalid)).not.toThrow();
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
    expect(useWorkflowHasChangesStore.getState().getSaveData).toBe(invalid);
    revision = useWorkflowYamlEditorStore.getState().revision;
    changes.setHasChanges(true);
    changes.setGetSaveData(() => previous);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(revision + 1);
  });
});

test("the legacy editor does not mount a second recording controller outside Copilot", () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  useRecordingStore.setState({ isRecording: true });
  workflowQueryMock.mockReturnValue({ data: liveWorkflow, isLoading: false });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/agents/wpid_live/edit"]}>
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/edit"
            element={
              <DebugStoreProvider>
                <ReactFlowProvider>
                  <Workspace
                    initialNodes={[]}
                    initialEdges={[]}
                    initialTitle="Live agent"
                    workflow={liveWorkflow}
                    showBrowser
                  />
                </ReactFlowProvider>
              </DebugStoreProvider>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  expect(screen.queryByTestId("standalone-recording-panel")).toBeNull();
  queryClient.clear();
});
