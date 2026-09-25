import { getClient } from "@/api/AxiosClient";
import { Workspace, useWorkspaceCopilotUpdate } from "../editor/Workspace";
import * as copilotChatModule from "./WorkflowCopilotChat";
import { FlowRenderer } from "../editor/FlowRenderer";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { DebugStoreContext } from "@/store/DebugStoreContext";
import {
  ACCEPT_SETTLE_CEILING_MS,
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
  type WorkflowUpdateOptions,
} from "./WorkflowCopilotChat";

import { WorkflowComparisonPanel } from "../editor/panels/WorkflowComparisonPanel";
import { WorkflowPermanentIdContext } from "../WorkflowPermanentIdContext";
import type { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import {
  cloneElement,
  useEffect,
  useLayoutEffect,
  useState,
  type ComponentProps,
} from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowScopeContext } from "../editor/WorkflowScopeContext";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import * as editorStateSnapshots from "../editor/editorStateSnapshot";
import { EditorView } from "@codemirror/view";
import { WorkflowYamlEditor } from "../editor/WorkflowYamlEditor";
import { EditableNodeTitle } from "../editor/nodes/components/EditableNodeTitle";
import { useDeferredTitleEdit } from "../hooks/useDeferredTitleEdit";
import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";
import type { WorkflowApiResponse } from "../types/workflowTypes";
import type { AppNode } from "../editor/nodes";
import {
  bindCopilotReviewClose,
  captureEditorState,
  restoreEditorState,
  type EditorStateSnapshot,
  type RestoreResult,
} from "../editor/editorStateSnapshot";
import { useNodeCollapseStore } from "../editor/collapse/useNodeCollapseStore";
import { getElements, getWorkflowBlocks } from "../editor/workflowEditorUtils";
import {
  useWorkflowHasChangesStore,
  useHydrateWorkflowParameters,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parse, stringify } from "yaml";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import {
  selectEditorMutationLocked,
  reconcileYamlDraftAfterGraphChange,
  isLockedByOther,
  refuseMutationDuringYamlCommit,
  beginSaveTransaction,
  finishSaveTransaction,
  registerEditorOwner,
  unregisterEditorOwner,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  beginYamlCommit,
  commitYamlDraft,
  createYamlCommitOwner,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { toast } from "@/components/ui/use-toast";
import { yamlCommitInputs } from "../editor/workflowVersionFromSaveData";
import { apiWorkflowToSettings } from "../editor/apiWorkflowToSettings";
import { buildWorkflowSaveRequest } from "../editor/workflowYamlDocument";
import { queryClient } from "@/api/QueryClient";
import bundles from "./narrativeState.turnFacts.fixture.json";
import type {
  WorkflowCopilotChatSummary,
  WorkflowCopilotStreamResponseUpdate,
} from "./workflowCopilotTypes";

import { TooltipProvider } from "@/components/ui/tooltip";
import { SaveButton } from "@/routes/workflows/studio/StudioTopBar";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useRecordingStore } from "@/store/useRecordingStore";

import {
  fenceBaselineFor,
  hydratedGateFailure,
  extendedClaimHold,
  unattributedClaimDeadline,
} from "./acceptFence";

const editOneOfTwoBundle = bundles["different-source-edit-one-of-two"];
const routeLocation = vi.hoisted(() => ({ pathname: "/" }));

type StreamBody = {
  message: string;
  workflow_yaml: string;
  cancel_token: string;
  keep_pending_proposal?: boolean;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
  signal?: AbortSignal;
};

const { streamCalls, postStreaming, cancelPost, historyGet, historyResponse } =
  vi.hoisted(() => {
    const calls: StreamCall[] = [];
    const post = vi.fn().mockResolvedValue({});
    const streaming = vi.fn(
      (
        _path: string,
        body: StreamBody,
        onMessage: (payload: unknown) => boolean,
        options?: { signal?: AbortSignal },
      ) =>
        new Promise<void>((resolve, reject) => {
          options?.signal?.addEventListener("abort", () => resolve(), {
            once: true,
          });
          calls.push({
            body,
            onMessage,
            resolve,
            reject,
            signal: options?.signal,
          });
        }),
    );
    const history = {
      data: {
        workflow_copilot_chat_id: "chat-1" as string | null,
        request_turn_id: null as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null as Record<string, unknown> | null,
        proposed_workflow_metadata: null as {
          owner_turn_id: string;
          revision: number;
          canonical_fingerprint: string;
          disposition: "review_untested" | "accepting";
          claimed_at?: string | null;
          workflow_run_id: string | null;
        } | null,
        proposed_claim_expires_in_seconds: null as number | null | undefined,
        proposed_workflow_run: null as {
          workflow_run_id: string;
          status: string | null;
          available: boolean;
          failure_reason: string | null;
          outputs: Array<{ output_parameter_id: string; value: unknown }>;
        } | null,
        auto_accept: false as boolean,
        question_interactions: [] as unknown[],
      },
    };
    const get = vi.fn().mockImplementation(() => Promise.resolve(history));
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyGet: get,
      historyResponse: history,
    };
  });

vi.mock("../editor/FlowRenderer", () => ({ FlowRenderer: vi.fn(() => null) }));

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: historyGet,
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

const speech = vi.hoisted(() => ({
  stop: vi.fn(),
  takeAudioBlob: vi.fn(),
  isListening: false,
}));
vi.mock("@/hooks/useSpeechToTextField", () => ({
  useSpeechToTextField: () => ({
    ...speech,
    isSupported: true,
    toggle: vi.fn(),
  }),
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => ({
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined,
    }),
    useSearchParams: () => [new URLSearchParams(), vi.fn()],
    useNavigate: () => vi.fn(),
    useLocation: () => ({
      pathname: routeLocation.pathname,
      search: "",
      hash: "",
      state: null,
      key: "default",
    }),
  };
});

const saveData = {
  title: "Test WF",
  description: "Unsaved description",
  workflow: {
    workflow_id: "wf_1",
    workflow_permanent_id: "wpid_1",
    description: "",
    totp_verification_url: null,
    is_saved_task: false,
    status: "published",
  },
  settings: {
    proxyLocation: null,
    webhookCallbackUrl: null,
    persistBrowserSession: false,
    pinSavedSessionIp: false,
    browserProfileId: null,
    browserProfileKey: null,
    model: null,
    maxScreenshotScrolls: null,
    extraHttpHeaders: null,
    cdpConnectHeaders: '{"Authorization":"********"}',
    totpIdentifier: "unsaved-totp",
    totpVerificationUrl: "https://example.test/totp",
    adaptiveCaching: true,
    generateScriptOnTerminal: false,
    maxElapsedTimeMinutes: 25,
    maskSecrets: true,
    runWith: "agent",
    scriptCacheKey: "",
    aiFallback: true,
    codeVersion: 2,
    runSequentially: false,
    sequentialKey: null,
  },
  parameters: [],
  blocks: [],
  workflowDefinitionVersion: 1,
} as unknown as WorkflowSaveData;

const initialSaveData = structuredClone(saveData);
// The real SaveButton renders beside the chat, so the save-block handoff runs through
// the real store; only its save hook is stubbed.
vi.mock("@/routes/workflows/editor/hooks/useSaveWorkflow", () => ({
  useSaveWorkflow: () => vi.fn(() => Promise.resolve()),
}));

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));
vi.mock("@/routes/workflows/editor/recording/RecordingPanel", () => ({
  RecordingPanel: () => <div data-testid="recording-chapter" />,
}));

let editorNodes: AppNode[] = [];
const setEditorNodes = vi.fn((nodes: AppNode[]) => {
  editorNodes = nodes;
});
const restoreLive = (snapshot: EditorStateSnapshot): RestoreResult =>
  restoreEditorState(snapshot, {
    workflowPermanentId: "wpid_1",
    setNodes: setEditorNodes,
    setEdges: vi.fn(),
    parametersStore: useWorkflowParametersStore.getState(),
    titleStore: useWorkflowTitleStore.getState(),
    changesStore: useWorkflowHasChangesStore.getState(),
    collapseStore: useNodeCollapseStore.getState(),
    restoreOwnership: (workflowPermanentId) => {
      useWorkflowParametersStore.setState({
        parametersWorkflowPermanentId: workflowPermanentId,
      });
      useWorkflowTitleStore.setState({
        titleWorkflowPermanentId: workflowPermanentId,
        descriptionWorkflowPermanentId: workflowPermanentId,
      });
    },
    scheduleLayout: vi.fn(),
    isLockedByOther,
  });
type ChatProps = NonNullable<Parameters<typeof WorkflowCopilotChat>[0]>;

async function renderChat(
  props: {
    route?: "editor" | "debugger";
    docked?: boolean;
    isOpen?: boolean;
    onWorkflowUpdate?: NonNullable<
      ComponentProps<typeof WorkflowCopilotChat>
    >["onWorkflowUpdate"];
    onRestore?: (snapshot: EditorStateSnapshot) => RestoreResult;
    beforeRecovery?: () => void;
    onReviewWorkflow?: NonNullable<
      ComponentProps<typeof WorkflowCopilotChat>
    >["onReviewWorkflow"];
    requiresLiveBrowser?: boolean;
    isLiveBrowserReady?: boolean;
    liveBrowserSessionId?: string | null;
    workflowPermanentId?: string;
  } = {},
) {
  const workflowPermanentId = props.workflowPermanentId ?? "wpid_1";
  const currentOwner = useWorkflowYamlEditorStore.getState().editorOwner;
  const owner =
    currentOwner?.active &&
    currentOwner.workflowPermanentId === workflowPermanentId
      ? currentOwner
      : createYamlCommitOwner(workflowPermanentId);
  registerEditorOwner(owner);
  routeLocation.pathname = props.route
    ? `/agents/wpid_1/${props.route === "debugger" ? "debug" : "edit"}`
    : "/";
  // docked renders via a portal; without a target it intentionally renders null.
  const portalTarget = props.docked ? document.body : undefined;
  editorNodes = [
    {
      id: "start",
      type: "start",
      position: { x: 0, y: 0 },
      data: { ...saveData.settings },
    },
    {
      id: "loop",
      type: "loop",
      position: { x: 0, y: 0 },
      data: {
        label: "Loop",
        loopKind: "for_each",
        loopValue: "unsaved_items",
        loopVariableReference: "{{ item }}",
      },
    },
  ] as AppNode[];
  const chat = (
    <WorkflowCopilotChat
      captureEditorState={() => {
        const titles = useWorkflowTitleStore.getState();
        const changes = useWorkflowHasChangesStore.getState();
        return captureEditorState({
          workflowPermanentId: "wpid_1",
          nodes: editorNodes,
          edges: [],
          parameters: useWorkflowParametersStore.getState().parameters,
          title: titles.title,
          titleHasBeenGenerated: titles.titleHasBeenGenerated,
          description: titles.description,
          hasChanges: changes.hasChanges,
          saveGeneration: changes.saveGeneration,
        });
      }}
      restoreEditorState={props.onRestore ?? restoreLive}
      onWorkflowPersisted={(workflowPermanentId) =>
        useWorkflowHasChangesStore
          .getState()
          .recordPersistedSave(workflowPermanentId)
      }
      onReviewWorkflow={props.onReviewWorkflow}
      onWorkflowUpdate={(workflow, options) => {
        if (typeof workflow.title === "string") {
          if (options?.midTurnDraft)
            useWorkflowTitleStore
              .getState()
              .setTitleFromCopilotIfDefault(workflow.title);
          else
            useWorkflowTitleStore
              .getState()
              .syncTitleFromWorkflow(workflow.title);
        }
        props.onWorkflowUpdate?.(workflow, options);
        if (workflow.workflow_definition?.blocks)
          editorNodes = getElements(
            workflow.workflow_definition.blocks,
            options?.settings ?? apiWorkflowToSettings(workflow),
            true,
          ).nodes;
      }}
      requiresLiveBrowser={props.requiresLiveBrowser}
      isLiveBrowserReady={props.isLiveBrowserReady}
      liveBrowserSessionId={props.liveBrowserSessionId}
      isOpen={props.isOpen}
      {...Object.fromEntries(
        Object.entries(props).filter(
          ([key]) =>
            ![
              "onWorkflowUpdate",
              "onRestore",
              "route",
              "beforeRecovery",
            ].includes(key),
        ),
      )}
      docked={props.docked ?? false}
      portalTarget={portalTarget}
    />
  );
  const view = render(chat, {
    wrapper: function EditorWrapper({ children }) {
      useLayoutEffect(() => props.beforeRecovery?.(), []);
      return (
        <WorkflowPermanentIdContext.Provider value={props.workflowPermanentId}>
          {children}
        </WorkflowPermanentIdContext.Provider>
      );
    },
  });
  if (props.isOpen === false) {
    await act(async () => {});
  } else if (vi.isFakeTimers()) {
    await act(async () => {});
    expect(screen.getByRole("textbox")).toBeTruthy();
  } else {
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  }
  return {
    ...view,
    autoSend: () =>
      view.rerender(
        cloneElement(chat, {
          initialMessage: "Keep my code and update the title",
        }),
      ),
    connectBrowser: () =>
      view.rerender(
        cloneElement(chat, {
          isLiveBrowserReady: true,
          liveBrowserSessionId: "pbs_live",
        }),
      ),
    unmount: () => {
      view.unmount();
      unregisterEditorOwner(owner);
    },
  };
}

function leaseDecrementingFrom(seconds: number | null | undefined): number {
  if (typeof seconds !== "number") {
    // null is a server saying no claim is held and undefined is one that cannot say. Neither
    // decrements, and the difference between them is load-bearing elsewhere.
    historyResponse.data.proposed_claim_expires_in_seconds = seconds;
    return 0;
  }
  const endsAt = Date.now() + seconds * 1000;
  Object.defineProperty(
    historyResponse.data,
    "proposed_claim_expires_in_seconds",
    {
      configurable: true,
      get: () => (endsAt - Date.now()) / 1000,
      set: (value: number | null | undefined) => {
        Object.defineProperty(
          historyResponse.data,
          "proposed_claim_expires_in_seconds",
          { configurable: true, enumerable: true, writable: true, value },
        );
      },
    },
  );
  return endsAt;
}

function textarea(): HTMLTextAreaElement {
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

async function submit(value: string) {
  fireEvent.change(textarea(), { target: { value } });
  await act(async () => {
    fireEvent.keyDown(textarea(), { key: "Enter" });
  });
}

function finishTurnHistory() {
  historyResponse.data.request_turn_id = "turn-1";
  historyResponse.data.chat_history = [
    {
      sender: "ai",
      content: "Turn finished",
      turn_outcome: { copilot_turn_id: "turn-1", terminal_reason: null },
    },
  ];
}

async function stageProposalOn(
  blockLabel: string,
  props: Partial<ChatProps> = {},
) {
  historyGet.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/workflows/wpid_1"
        ? {
            data: {
              ...saveData.workflow,
              workflow_id: "wf_canonical",
              version: 4,
            },
          }
        : historyResponse,
    ),
  );
  await renderChat(props);
  await submit("build me a workflow");
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
  await act(async () => {
    streamCalls[0]!.onMessage(
      proposalResponse("Draft ready.", {
        narrative_payload: proposalNarrativePayload({
          draft: { blockCount: 1, blockLabels: [blockLabel], summary: null },
        }),
        proposed_workflow_metadata: {
          owner_turn_id: "turn-1",
          revision: 1,
          canonical_fingerprint: "canonical-1",
          disposition: "review_untested",
          workflow_run_id: null,
        },
      }),
    );
    streamCalls[0]!.resolve();
  });
  expect(screen.getByTitle(blockLabel)).toBeTruthy();
}

async function acceptRefusedWith(status: number) {
  cancelPost.mockRejectedValueOnce({ response: { status } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
  });
}

const proposedWorkflowPayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  workflow_id: "wf_proposed",
  workflow_permanent_id: "wpid_1",
  workflow_definition: { blocks: [], parameters: [] },
  description: "Proposed description",
  title: "Draft workflow",
  _copilot_unvalidated: true,
  ...overrides,
});

const proposalNarrativePayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  responseType: "REPLY",
  cancelled: false,
  proposalDisposition: "review_untested",
  designStarted: true,
  designEnded: true,
  draft: {
    blockCount: 1,
    blockLabels: ["extract_titles"],
    summary: null,
  },
  blocks: [
    {
      workflowRunBlockId: "wrb_extract_titles",
      label: "extract_titles",
      blockType: "task",
      state: "completed",
      lastSeenIteration: 0,
      activity: [],
      startedAt: null,
      endedAt: null,
    },
  ],
  terminal: "response",
  terminalMessage: "Here is a draft workflow for you to review.",
  narrativeSummary: "Here is a draft workflow for you to review.",
  priorBlockCount: 0,
  designActivity: [],
  startedAt: "2026-07-09T00:00:00Z",
  endedAt: "2026-07-09T00:00:05Z",
  ...overrides,
});

const proposalResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: proposedWorkflowPayload(),
    response_time: "2026-07-09T00:00:05Z",
    proposal_disposition: "review_untested",
    turn_id: "turn-1",
    narrative_payload: proposalNarrativePayload(),
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

// The v1 (Ask-mode) backend streams no narrative frames at all: no turn_start,
// and a terminal frame carrying only the schema defaults for turn_id and
// narrative_payload. It never stamps _copilot_unvalidated either, so its
// proposal reads Tested.
const legacyProposalResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: {
      workflow_id: "wf_proposed",
      title: "Draft workflow",
    },
    response_time: "2026-07-09T00:00:05Z",
    total_tokens: null,
    response_type: "REPLY",
    proposal_disposition: "auto_applicable",
    workflow_applied: false,
    cancelled: false,
    output_policy_diagnostics: null,
    turn_id: null,
    narrative_summary: null,
    narrative_payload: null,
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

const plainReplyResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: null,
    response_time: "2026-07-09T00:00:10Z",
    proposal_disposition: "no_proposal",
    turn_id: "turn-2",
    narrative_payload: {
      ...proposalNarrativePayload(),
      turnId: "turn-2",
      turnIndex: 1,
      draft: null,
      proposalDisposition: "no_proposal",
      terminalMessage: message,
      narrativeSummary: message,
    },
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

beforeEach(() => {
  routeLocation.pathname = "/";
  useWorkflowSnapshotStore.getState().clearSnapshot();
  clearDeferredEdits();
  Object.assign(saveData, structuredClone(initialSaveData));
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState({
    ...useWorkflowHasChangesStore.getInitialState(),
    getSaveData: () => saveData,
    hasChanges: false,
  });
  useWorkflowTitleStore.setState({
    ...useWorkflowTitleStore.getInitialState(),
    title: saveData.title,
    description: saveData.description,
    titleHasBeenGenerated: true,
  });
  useWorkflowParametersStore.setState({
    parameters: [
      {
        parameterType: "context",
        key: "context",
        sourceParameterKey: "unsaved_source",
      },
    ],
  });
  useNodeCollapseStore.setState({ collapsed: {} });
  setEditorNodes.mockClear();
  sessionStorage.clear();
  vi.mocked(toast).mockClear();
  vi.spyOn(useWorkflowHasChangesStore.getState(), "setHasChanges");
  speech.stop.mockReset();
  speech.takeAudioBlob.mockReset();
  speech.isListening = false;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockReset();
  cancelPost.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/workflow/copilot/apply-proposed-workflow"
        ? { data: proposedWorkflowPayload() }
        : {},
    ),
  );
  historyGet.mockReset();
  historyGet.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/workflows/wpid_1"
        ? { data: saveData.workflow }
        : historyResponse,
    ),
  );
  saveData.settings.cdpConnectHeaders = '{"Authorization":"********"}';
  saveData.settings.extraHttpHeaders = null;
  historyResponse.data = {
    workflow_copilot_chat_id: "chat-1",
    request_turn_id: null,
    chat_history: [],
    proposed_workflow: null,
    proposed_workflow_metadata: null,
    proposed_claim_expires_in_seconds: null as number | null | undefined,
    proposed_workflow_run: null,
    auto_accept: false,
    question_interactions: [],
  };
});

afterEach(() => {
  queryClient.clear();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
  queryClient.clear();
  vi.useRealTimers();
});

const changesState = {
  get hasChanges() {
    return useWorkflowHasChangesStore.getState().hasChanges;
  },
  set hasChanges(value: boolean) {
    useWorkflowHasChangesStore.setState({ hasChanges: value });
  },
  get setHasChanges() {
    return vi.mocked(useWorkflowHasChangesStore.getState().setHasChanges);
  },
};

function recoveredQuestion() {
  return {
    interaction_id: "question-recovered",
    turn_id: "turn-recovered",
    tool_call_id: "call-recovered",
    status: "pending" as const,
    response: null,
    created_at: new Date().toISOString(),
    resolved_at: null,
    parts: [{ part_id: "format", prompt: "Which format?", choices: [] }],
  };
}

function pausedHistory(kind = "question") {
  return {
    ...historyResponse.data,
    question_interactions: kind === "question" ? [recoveredQuestion()] : [],
    pending_question_cancel_token:
      kind === "question" ? "original-cancel-token" : null,
    pending_credential_requests:
      kind === "credential"
        ? [
            {
              type: "credential_required",
              turn_id: "turn-recovered",
              workflow_copilot_chat_id: "chat-1",
              resume_token: "resume",
              reason: "workflow_credential_inputs_unbound",
              message: "",
              login_page_urls: ["https://example.test/login"],
              credential_refs: [],
              timeout_seconds: 300,
              expires_at: new Date(Date.now() + 300_000).toISOString(),
              timestamp: new Date().toISOString(),
            },
          ]
        : [],
    request_turn_id: kind === "credential" ? "turn-recovered" : null,
    request_cancel_token:
      kind === "credential" ? "original-cancel-token" : null,
    chat_history:
      kind === "credential"
        ? []
        : [
            {
              sender: "ai",
              content: "Waiting for your answer",
              created_at: new Date().toISOString(),
              turn_outcome: {
                copilot_turn_id: "turn-recovered",
                terminal_reason: "interrupted",
                request_cancel_token: "original-cancel-token",
              },
            },
          ],
  };
}

function completedHistory() {
  return {
    ...historyResponse.data,
    question_interactions: [],
    chat_history: [
      {
        sender: "ai",
        content: "Continuation completed",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-recovered",
          terminal_reason: null,
          request_cancel_token: "original-cancel-token",
        },
      },
    ],
  };
}

describe("WorkflowCopilotChat — g2 review gate", () => {
  it("does not pin ordinary history loading to a retained request token", async () => {
    sessionStorage.setItem(
      "copilot-request-cancel-token:wpid_1",
      "older-request",
    );
    await renderChat();
    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(historyGet.mock.calls[0]?.[1]?.params).not.toHaveProperty(
      "request_cancel_token",
    );
  });

  it.each([true, false])(
    "blocks chat switching during Accept recovery (other chat proposal: %s)",
    async (hasProposal) => {
      const baseline = {
        ...saveData.workflow,
        version: 1,
        modified_at: "2026-09-01T00:00:00Z",
      };
      saveData.workflow = baseline;
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      const apply = vi.fn();
      await renderChat({ docked: true, onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      let fail!: (error: Error) => void;
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            fail = reject;
          }),
      );
      await act(async () => fireEvent.click(accept));
      const canonical = { ...baseline, workflow_id: "wf_accepted", version: 2 };
      const other = {
        ...historyResponse.data,
        workflow_copilot_chat_id: "chat-2",
        proposed_workflow: hasProposal
          ? proposedWorkflowPayload({ title: "Other proposal" })
          : null,
      };
      historyGet.mockImplementation(
        (
          path: string,
          config?: { params?: { workflow_copilot_chat_id?: string } },
        ) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: canonical }
              : {
                  data:
                    config?.params?.workflow_copilot_chat_id === "chat-2"
                      ? other
                      : { ...historyResponse.data, proposed_workflow: null },
                },
          ),
      );
      vi.useFakeTimers();
      await act(async () => fail(new Error("response lost")));
      await act(async () =>
        useCopilotHeaderStore.getState().controls!.onSelectChat({
          workflow_copilot_chat_id: "chat-2",
        } as WorkflowCopilotChatSummary),
      );
      expect(
        screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
      ).toBe(true);
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).toHaveBeenCalledWith(
        canonical,
        expect.objectContaining({ persisted: true }),
      );
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it.each([
    "delayed commit",
    "empty version",
    "live claim",
    "markerless accepting",
    "filled temporary version",
    "empty baseline",
    "clock ahead",
    "empty completed definition",
    "different proposal",
    "pre-existing expired claim",
    "unreported cleared proposal",
    "live hidden proposal",
  ])(
    "keeps uncertain Accept reserved until completed evidence: %s",
    async (phase) => {
      const baseline = {
        ...saveData.workflow,
        version: 1,
        modified_at: "2026-09-01T00:00:00Z",
        workflow_definition: {
          parameters: [],
          blocks: [{ block_type: "task", label: "before" }],
        },
      } as unknown as WorkflowApiResponse;
      if (phase === "empty baseline") baseline.workflow_definition.blocks = [];
      saveData.workflow = baseline;
      const canonical = {
        ...baseline,
        workflow_id: "wf_complete",
        version: 2,
        modified_at: "2026-09-02T00:00:00Z",
        workflow_definition: {
          parameters: [],
          blocks: [{ block_type: "task", label: "after" }],
        },
      } as unknown as WorkflowApiResponse;
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-accept",
        revision: 3,
        disposition: "review_untested",
        canonical_fingerprint: "before",
        workflow_run_id: null,
      };
      if (phase === "pre-existing expired claim") {
        historyResponse.data.proposed_workflow_metadata = {
          ...historyResponse.data.proposed_workflow_metadata!,
          disposition: "accepting",
          claimed_at: new Date(Date.now() - 301_000).toISOString(),
        };
      }
      const apply = vi.fn();
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      cancelPost.mockRejectedValueOnce(new Error("response lost"));
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      historyResponse.data.proposed_workflow_metadata = {
        ...historyResponse.data.proposed_workflow_metadata!,
        disposition: "accepting",
        claimed_at:
          phase === "pre-existing expired claim"
            ? historyResponse.data.proposed_workflow_metadata!.claimed_at
            : new Date().toISOString(),
        ...(phase === "different proposal" ? { revision: 4 } : {}),
      };
      let saved =
        phase === "empty version" || phase === "empty baseline"
          ? {
              ...canonical,
              workflow_definition: { parameters: [], blocks: [] },
            }
          : baseline;
      if (
        phase === "filled temporary version" ||
        phase === "different proposal"
      )
        saved = canonical;
      if (phase === "markerless accepting")
        historyResponse.data.proposed_workflow_metadata!.claimed_at = null;
      if (phase === "clock ahead") vi.setSystemTime(Date.now() + 360_000);
      if (phase === "empty completed definition")
        canonical.workflow_definition.blocks = [];
      if (
        phase === "unreported cleared proposal" ||
        phase === "live hidden proposal"
      ) {
        historyResponse.data.proposed_workflow = null;
        historyResponse.data.proposed_workflow_metadata = null;
        historyResponse.data.proposed_claim_expires_in_seconds =
          phase === "live hidden proposal" ? 120 : undefined;
      }
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: saved } : historyResponse,
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).not.toHaveBeenCalled();
      expect(beginSaveTransaction(owner)).toBe(false);
      saved = canonical;
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true }),
      );
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each([false, true])(
    "keeps an unchanged proposal reserved (canonical changed: %s)",
    async (changed) => {
      const baseline = saveData.workflow;
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-accept",
        revision: 1,
        canonical_fingerprint: "before",
        disposition: "review_untested",
        workflow_run_id: null,
      };
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      cancelPost.mockRejectedValueOnce(new Error("lost response"));
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      historyResponse.data.proposed_workflow_metadata = {
        ...historyResponse.data.proposed_workflow_metadata!,
        claimed_at: null,
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: changed ? { ...baseline, version: 2 } : baseline }
            : historyResponse,
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({ title: "Accept failed" }),
      );
    },
  );

  it.each([false, true])(
    "keeps waiting after an interrupted generation (canonical changed: %s)",
    async (changed) => {
      const baseline = {
        ...saveData.workflow,
        version: 1,
        modified_at: "2026-09-01T00:00:00Z",
      };
      saveData.workflow = baseline;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      await act(async () =>
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-abandoned",
          turn_index: 0,
        }),
      );
      const canonical = changed
        ? { ...baseline, workflow_id: "wf_committed", version: 2 }
        : baseline;
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Generation was interrupted",
          created_at: new Date().toISOString(),
          turn_outcome: {
            copilot_turn_id: "turn-abandoned",
            terminal_reason: "interrupted",
          },
        },
      ];
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      vi.useFakeTimers();
      await act(async () => streamCalls[0]!.reject(new Error("stream lost")));
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(screen.getByText("Generation was interrupted")).toBeTruthy();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(apply).not.toHaveBeenCalled();
    },
  );

  it("bounds a hanging Accept and keeps Retry and Reload available until settlement", async () => {
    const baseline = {
      ...saveData.workflow,
      version: 1,
      modified_at: "2026-09-01T00:00:00Z",
    };
    saveData.workflow = baseline;
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const accept = await screen.findByRole("button", { name: "Accept" });
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    cancelPost.mockImplementationOnce(() => new Promise(() => {}));
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: baseline } : historyResponse,
      ),
    );
    vi.useFakeTimers();
    await act(async () => fireEvent.click(accept));
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(beginSaveTransaction(owner)).toBe(false);
    await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    const canonical = { ...baseline, workflow_id: "wf_accepted", version: 2 };
    historyResponse.data.proposed_workflow = null;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apply).toHaveBeenCalledWith(
      canonical,
      expect.objectContaining({ persisted: true }),
    );
    expect(beginSaveTransaction(owner)).toBe(true);
  });

  it("bounds an unreadable Accept resync without releasing its reservation", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-accept",
      revision: 1,
      canonical_fingerprint: "before",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();
    const accept = await screen.findByRole("button", { name: "Accept" });
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
    historyGet.mockImplementation(() => new Promise(() => {}));
    vi.useFakeTimers();
    await act(async () => fireEvent.click(accept));
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(beginSaveTransaction(owner)).toBe(false);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it.each(["success", "lost response"])(
    "invalidates disposed Accept caches on %s and refetches on reopen",
    async (outcome) => {
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({ workflow_id: "wf_accepted" }),
      };
      historyResponse.data.proposed_workflow = canonical;
      const keys = [
        ["workflow", "wpid_1"],
        ["workflows"],
        ["block-scripts", "wpid_1"],
      ];
      for (const key of keys) queryClient.setQueryData(key, saveData.workflow);
      const apply = vi.fn();
      const view = await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole(
        "button",
        { name: "Accept" },
        { timeout: 10_000 },
      );
      let settle!: (value: unknown) => void;
      let fail!: (error: Error) => void;
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            settle = resolve;
            fail = reject;
          }),
      );
      await act(async () => fireEvent.click(accept));
      await waitFor(() => expect(cancelPost).toHaveBeenCalledOnce(), {
        timeout: 10_000,
      });
      view.unmount();
      await act(async () => {
        if (outcome === "success") settle({ data: canonical });
        else fail(new Error("response lost after commit"));
      });
      expect(apply).not.toHaveBeenCalled();
      for (const key of keys)
        expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
      const fetch = vi.fn().mockResolvedValue(canonical);
      expect(
        await queryClient.fetchQuery({ queryKey: keys[0]!, queryFn: fetch }),
      ).toEqual(canonical);
      expect(fetch).toHaveBeenCalledOnce();
      queryClient.clear();
    },
  );

  it.each(["legacy", "metadata"])(
    "retains a %s proposal without local application after Accept returns 400",
    async (kind) => {
      const proposal = proposedWorkflowPayload({
        workflow_definition: { parameters: [], blocks: [] },
      });
      historyResponse.data.proposed_workflow = proposal;
      if (kind === "metadata") {
        historyResponse.data.proposed_workflow_metadata = {
          owner_turn_id: "turn-proposed",
          revision: 1,
          canonical_fingerprint: "baseline",
          disposition: "review_untested",
          workflow_run_id: null,
        };
      }
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      historyGet.mockClear();
      cancelPost.mockRejectedValueOnce({ response: { status: 400 } });

      await act(async () => fireEvent.click(accept));

      expect(apply).not.toHaveBeenCalled();
      expect(cancelPost).toHaveBeenCalledOnce();
      expect(screen.getByText("Not saved")).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
      ).toBe(false);
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each([
    "interrupted commit",
    "legacy commit",
    "legacy unchanged",
    "500 unchanged",
    "cleared unchanged",
    "pending newer commit",
  ])("settles uncertain Accept from proposal evidence: %s", async (outcome) => {
    const baseline = {
      ...saveData.workflow,
      version: 1,
      modified_at: "2026-09-01T00:00:00Z",
    };
    saveData.workflow = baseline;
    const canonical = {
      ...baseline,
      ...proposedWorkflowPayload({ workflow_id: "wf_accepted" }),
      version: 2,
      modified_at: "2026-09-02T00:00:00Z",
    };
    historyResponse.data.proposed_workflow = canonical;
    if (!outcome.startsWith("legacy")) {
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-recovered",
        revision: 1,
        canonical_fingerprint: "baseline",
        disposition: "review_untested",
        workflow_run_id: null,
      };
      historyResponse.data.chat_history = pausedHistory().chat_history;
    }
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const accept = await screen.findByRole(
      "button",
      { name: "Accept" },
      { timeout: 10_000 },
    );
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    cancelPost.mockRejectedValueOnce(
      Object.assign(new Error("accept failed"), {
        response: { status: 500 },
      }),
    );
    if (["500 unchanged", "pending newer commit"].includes(outcome)) {
      historyResponse.data.proposed_workflow_metadata = {
        ...historyResponse.data.proposed_workflow_metadata!,
        disposition: "accepting",
        claimed_at: new Date(Date.now() - 301_000).toISOString(),
      };
    }
    const committed =
      outcome.endsWith("commit") || outcome === "cleared unchanged";
    const saved = outcome === "cleared unchanged" ? baseline : canonical;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: committed ? saved : baseline }
          : historyResponse,
      ),
    );
    historyGet.mockClear();
    vi.useFakeTimers();
    await act(async () => fireEvent.click(accept));
    expect(apply).not.toHaveBeenCalled();
    expect(historyGet).not.toHaveBeenCalled();
    expect(beginSaveTransaction(owner)).toBe(false);
    if (committed && outcome !== "pending newer commit")
      historyResponse.data.proposed_workflow = null;
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    if (
      ["legacy unchanged", "500 unchanged", "pending newer commit"].includes(
        outcome,
      )
    ) {
      expect(apply).not.toHaveBeenCalled();
      expect(beginSaveTransaction(owner)).toBe(false);
      await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
      expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
      expect(beginSaveTransaction(owner)).toBe(false);
      return;
    }
    expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
      "/workflow/copilot/chat-history",
      "/workflows/wpid_1",
    ]);
    expect(historyGet.mock.calls[0]?.[1]?.params).toEqual({
      workflow_copilot_chat_id: "chat-1",
    });
    if (committed)
      expect(apply).toHaveBeenCalledWith(
        saved,
        expect.objectContaining({ persisted: true }),
      );
    else {
      expect(apply).not.toHaveBeenCalled();
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Accept failed",
          variant: "destructive",
        }),
      );
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    }
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(beginSaveTransaction(owner)).toBe(true);
  });

  it.each(["canonical", "proposal"])(
    "retains uncertain Accept only while the %s read fails, then Retry settles",
    async (failedRead) => {
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({ workflow_id: "wf_accepted", version: 2 }),
      };
      historyResponse.data.proposed_workflow = canonical;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole(
        "button",
        { name: "Accept" },
        { timeout: 10_000 },
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      cancelPost.mockRejectedValueOnce(new Error("Accept response lost"));
      historyGet.mockImplementation((path: string) => {
        if ((path === "/workflows/wpid_1") === (failedRead === "canonical"))
          return Promise.reject(new Error("read unavailable"));
        return Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        );
      });
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
      expect(apply).not.toHaveBeenCalled();
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
      await act(async () => {
        fireEvent.click(
          within(
            screen.getByText(/Could not confirm whether Copilot saved changes/)
              .parentElement!,
          ).getByRole("button", { name: "Retry" }),
        );
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(cancelPost).toHaveBeenCalledTimes(1);
      expect(beginSaveTransaction(owner)).toBe(false);
      historyResponse.data.proposed_workflow = null;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true }),
      );
      expect(cancelPost).toHaveBeenCalledTimes(1);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each(
    [
      "question",
      "credential",
      "external question",
      "external credential",
    ].flatMap((path) =>
      ["Keep my edits", "Apply and discard my edits"].map((choice) => [
        path,
        choice,
      ]),
    ),
  )(
    "protects local inputs before claiming delayed %s history: %s",
    async (path, choice) => {
      changesState.hasChanges = false;
      let historyLoaded!: (value: unknown) => void;
      historyGet.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            historyLoaded = resolve;
          }),
      );
      const apply = vi.fn(() =>
        useWorkflowParametersStore.getState().setParameters([]),
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      await renderChat({ onWorkflowUpdate: apply });
      const input = {
        key: "local_input",
        parameterType: "context" as const,
        sourceParameterKey: "source",
      };
      useWorkflowParametersStore.getState().setParameters([input]);
      useWorkflowTitleStore.getState().setTitle("Local title");
      useWorkflowYamlEditorStore.getState().open("title: Baseline");
      useWorkflowYamlEditorStore.getState().setDraft("title: Local YAML");
      changesState.hasChanges = true;
      vi.useFakeTimers();
      await act(async () =>
        historyLoaded({
          data: pausedHistory(
            path?.includes("credential") ? "credential" : "question",
          ),
        }),
      );
      if (!path?.startsWith("external")) {
        cancelPost.mockResolvedValueOnce({
          data: { ...recoveredQuestion(), status: "resolved" },
        });
        await act(async () =>
          fireEvent.click(
            screen.getByRole("button", {
              name: path === "credential" ? "Skip for now" : "Skip",
            }),
          ),
        );
      }
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({
          workflow_id: "wf_resumed",
          title: "New Workflow",
          workflow_definition: { parameters: [], blocks: [] },
        }),
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve({
          data: path === "/workflows/wpid_1" ? canonical : completedHistory(),
        }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).not.toHaveBeenCalled();
      expect(useWorkflowParametersStore.getState().parameters).toEqual([input]);
      expect(useWorkflowTitleStore.getState().title).toBe("Local title");
      expect(useWorkflowYamlEditorStore.getState().draft).toBe(
        "title: Local YAML",
      );
      expect(beginSaveTransaction(owner)).toBe(false);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: choice }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      if (choice === "Keep my edits") {
        expect(apply).not.toHaveBeenCalled();
        expect(useWorkflowParametersStore.getState().parameters).toEqual([
          input,
        ]);
        expect(useWorkflowTitleStore.getState().title).toBe("Local title");
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(
          "title: Local YAML",
        );
      } else {
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({ persisted: true }),
        );
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
        expect(useWorkflowTitleStore.getState().title).toBe(canonical.title);
        expect(
          useWorkflowTitleStore.getState().copilotMetadataEdits["wpid_1"]
            ?.edits,
        ).toEqual({});
        const yaml = useWorkflowYamlEditorStore.getState();
        expect(parse(yaml.draft)).toMatchObject({ title: canonical.title });
        expect(yaml.entrySnapshot).toBe(yaml.draft);
        expect(yaml.stale).toBe(false);
      }
    },
  );

  it.each(["question", "credential"])(
    "recovery Reject sends the original %s cancellation token and chat",
    async (kind) => {
      changesState.hasChanges = false;
      if (kind === "credential") {
        const first = await renderChat();
        await submit("Sign in");
        const token = streamCalls[0]!.body.cancel_token;
        first.unmount();
        historyGet.mockImplementation((_path, config) =>
          Promise.resolve({
            data: {
              ...pausedHistory(kind),
              request_turn_id:
                config?.params?.request_cancel_token === token
                  ? "turn-recovered"
                  : null,
            },
          }),
        );
      } else historyGet.mockResolvedValue({ data: pausedHistory(kind) });
      await renderChat();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Reject" })),
      );
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        {
          cancel_token:
            kind === "credential"
              ? streamCalls[0]!.body.cancel_token
              : "original-cancel-token",
          workflow_copilot_chat_id: "chat-1",
          source: "stop_button",
        },
        expect.anything(),
      );
    },
  );

  it("enables later questions when the answer POST settles after recovery", async () => {
    changesState.hasChanges = false;
    let loadHistory!: (value: unknown) => void;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          loadHistory = resolve;
        }),
    );
    await renderChat();
    vi.useFakeTimers();
    await act(async () => loadHistory({ data: pausedHistory() }));
    let finishAnswer!: (value: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAnswer = resolve;
        }),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Skip" })),
    );
    const canonical = {
      ...saveData.workflow,
      ...proposedWorkflowPayload({ workflow_id: "wf_resumed" }),
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve({
        data: path === "/workflows/wpid_1" ? canonical : completedHistory(),
      }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await act(async () =>
      finishAnswer({ data: { ...recoveredQuestion(), status: "resolved" } }),
    );
    vi.useRealTimers();
    await submit("continue");
    await act(async () =>
      streamCalls[0]!.onMessage({
        type: "question_required",
        turn_id: "turn-next",
        workflow_copilot_chat_id: "chat-1",
        cancel_token: "next-token",
        interactions: [
          {
            ...recoveredQuestion(),
            interaction_id: "question-next",
            turn_id: "turn-next",
          },
        ],
      }),
    );
    expect(
      (screen.getByRole("button", { name: "Skip" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it.each([401, 422])(
    "releases an HTTP %i rejection before turn_start without polling",
    async (status) => {
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      historyGet.mockClear();
      vi.useFakeTimers();
      await act(async () =>
        streamCalls[0]!.reject(
          Object.assign(new Error("Request rejected"), {
            status,
            body: '{"detail":"Request rejected"}',
          }),
        ),
      );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.getByText("Request rejected")).toBeTruthy();
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(historyGet).not.toHaveBeenCalled();
      expect(apply).not.toHaveBeenCalled();
    },
  );

  it.each([false, true])(
    "keeps a committed proposal when the Workspace apply fails (after Stop: %s)",
    async (afterStop) => {
      const canonical = {
        ...saveData.workflow,
        title: "Committed title",
        workflow_definition: { parameters: [], blocks: [] },
        workflow_id: "wf_committed",
        version: 2,
      } as WorkflowApiResponse;
      const applyGraph = vi.fn((): void => {
        throw new Error("Canvas unavailable");
      });
      const { result } = renderHook(() =>
        useWorkspaceCopilotUpdate({
          applyWorkflowUpdate: applyGraph,
        }),
      );
      await renderChat({ onWorkflowUpdate: result.current });
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await submit("edit the workflow");
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      vi.useFakeTimers();
      if (afterStop) {
        cancelPost.mockImplementationOnce(() => new Promise(() => {}));
        await act(async () => fireEvent.keyDown(textarea(), { key: "Escape" }));
      }
      await act(async () => {
        streamCalls[0]!.onMessage(
          proposalResponse("Saved", {
            updated_workflow: canonical,
            workflow_applied: true,
            proposal_disposition: "auto_applicable",
          }),
        );
        streamCalls[0]!.resolve();
      });
      expect(applyGraph).toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Update failed" }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
      applyGraph.mockImplementation(() => {});
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(applyGraph).toHaveBeenLastCalledWith(
        canonical,
        expect.objectContaining({
          persisted: true,
          applied: true,
          userDriven: true,
        }),
      );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    },
  );

  it.each(["resolve", "reject", "client"])(
    "recovers a stalled Stop before a late cancel %s",
    async (lateResult) => {
      await renderChat();
      await submit("edit the workflow");
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      let resolveCancel!: (value: unknown) => void;
      let rejectCancel!: (error: Error) => void;
      const pending = new Promise((resolve, reject) => {
        resolveCancel = resolve;
        rejectCancel = reject;
      });
      if (lateResult === "client")
        vi.mocked(getClient).mockImplementationOnce(
          () => pending as ReturnType<typeof getClient>,
        );
      else cancelPost.mockReturnValueOnce(pending);
      vi.useFakeTimers();
      await act(async () => fireEvent.keyDown(textarea(), { key: "Escape" }));
      await act(async () => vi.advanceTimersByTimeAsync(14_999));
      expect(streamCalls[0]!.signal?.aborted).toBe(false);
      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(streamCalls[0]!.signal?.aborted).toBe(true);
      if (lateResult !== "client")
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          expect.any(Object),
          expect.objectContaining({
            timeout: 15_000,
            signal: streamCalls[0]!.signal,
          }),
        );
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      const notices = screen.getAllByText(/Reload to see/).length;
      const posts = cancelPost.mock.calls.length;
      await act(async () => {
        if (lateResult === "reject")
          rejectCancel(new Error("Late network failure"));
        else resolveCancel(lateResult === "client" ? { post: cancelPost } : {});
      });
      expect(screen.getAllByText(/Reload to see/)).toHaveLength(notices);
      expect(cancelPost).toHaveBeenCalledTimes(posts);
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
    },
  );

  it("keeps ownership when Workspace cannot restore a cancelled draft", async () => {
    changesState.hasChanges = true;
    const applyGraph = vi.fn();
    const { result } = renderHook(() =>
      useWorkspaceCopilotUpdate({
        applyWorkflowUpdate: applyGraph,
      }),
    );
    await renderChat({
      onWorkflowUpdate: result.current,
      onRestore: (snapshot) => {
        applyGraph();
        return restoreLive(snapshot);
      },
    });
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload({
          workflow_definition: { parameters: [], blocks: [] },
        }),
      });
    });
    expect(applyGraph).toHaveBeenCalledTimes(1);
    applyGraph.mockImplementation(() => {
      throw new Error("Rollback unavailable");
    });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    vi.useFakeTimers();
    await act(async () => fireEvent.keyDown(textarea(), { key: "Escape" }));
    await act(async () => streamCalls[0]!.resolve());
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Cancelled",
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "user_cancelled",
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(applyGraph.mock.calls.length).toBeGreaterThan(1);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    applyGraph.mockImplementation(() => {});
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("reconciles a lost manual Accept response before releasing Save", async () => {
    const canonical = {
      ...saveData.workflow,
      ...proposedWorkflowPayload({ workflow_id: "wf_committed" }),
    };
    historyResponse.data.proposed_workflow = canonical;
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-accepted",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    cancelPost.mockRejectedValueOnce(new Error("Accept response lost"));
    let finishRead!: (response: { data: Record<string, unknown> }) => void;
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? new Promise((resolve) => {
            finishRead = resolve;
          })
        : Promise.resolve(historyResponse),
    );
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    expect(beginSaveTransaction(owner)).toBe(false);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Proposal ready",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-accepted",
          terminal_reason: null,
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(finishRead).toBeDefined();
    expect(beginSaveTransaction(owner)).toBe(false);
    await act(async () => finishRead({ data: canonical }));
    expect(apply).toHaveBeenCalledWith(
      canonical,
      expect.objectContaining({ persisted: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(beginSaveTransaction(owner)).toBe(true);
  });

  it.each(["credential", "question"])(
    "keeps a recovered %s continuation reserved when Workspace apply fails",
    async (kind) => {
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({ workflow_id: "wf_resumed" }),
        workflow_definition: { parameters: [], blocks: [] },
      };
      const question = {
        interaction_id: "question-1",
        turn_id: "turn-resumed",
        tool_call_id: "call-1",
        status: "pending",
        response: null,
        created_at: new Date().toISOString(),
        resolved_at: null,
        parts: [{ part_id: "format", prompt: "Which format?", choices: [] }],
      };
      Object.assign(
        historyResponse.data,
        kind === "credential"
          ? {
              pending_credential_requests: [
                {
                  type: "credential_required",
                  turn_id: "turn-resumed",
                  workflow_copilot_chat_id: "chat-1",
                  resume_token: "resume",
                  reason: "workflow_credential_inputs_unbound",
                  message: "",
                  login_page_urls: ["https://example.test/login"],
                  credential_refs: [],
                  timeout_seconds: 300,
                  expires_at: new Date(Date.now() + 300_000).toISOString(),
                  timestamp: new Date().toISOString(),
                },
              ],
            }
          : {
              question_interactions: [question],
              pending_question_cancel_token: "cancel",
            },
      );
      changesState.hasChanges = false;
      const applyGraph = vi.fn((): void => {
        throw new Error("Canvas unavailable");
      });
      const { result } = renderHook(() =>
        useWorkspaceCopilotUpdate({
          applyWorkflowUpdate: applyGraph,
        }),
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      vi.useFakeTimers();
      await act(async () => {
        render(<WorkflowCopilotChat onWorkflowUpdate={result.current} />);
      });
      cancelPost.mockResolvedValueOnce({
        data: { ...question, status: "resolved", response: { skipped: true } },
      });
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () =>
        fireEvent.click(
          screen.getByRole("button", {
            name: kind === "credential" ? "Skip for now" : "Skip",
          }),
        ),
      );
      Object.assign(historyResponse.data, {
        pending_credential_requests: [],
        question_interactions: [],
      });
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Continuation completed",
          created_at: new Date().toISOString(),
          turn_outcome: {
            copilot_turn_id: "turn-resumed",
            terminal_reason: null,
          },
        },
      ];
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(applyGraph).toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      expect(beginSaveTransaction(owner)).toBe(false);
      applyGraph.mockImplementation(() => {});
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each(["unresolved chat", "HTTP rejection"])(
    "refuses local Accept fallback after %s",
    async (mode) => {
      const proposal = proposedWorkflowPayload({
        workflow_definition: { parameters: [], blocks: [] },
      });
      historyResponse.data.proposed_workflow = proposal;
      if (mode === "unresolved chat")
        historyResponse.data.workflow_copilot_chat_id = null;
      const apply = vi.fn((): void => {
        throw new Error("Canvas unavailable");
      });
      await renderChat({ onWorkflowUpdate: apply });
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      if (mode === "HTTP rejection")
        cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
      vi.useFakeTimers();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Accept" })),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(
        screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
      ).toBe(mode === "HTTP rejection");
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance === null,
      ).toBe(mode === "unresolved chat");
      expect(
        cancelPost.mock.calls.some(
          ([path]) => path === "/workflow/copilot/clear-proposed-workflow",
        ),
      ).toBe(false);
    },
  );

  it("retains a manually accepted proposal if Workspace cannot apply the saved version", async () => {
    const canonical = {
      ...saveData.workflow,
      workflow_id: "wf_committed",
      workflow_definition: { parameters: [], blocks: [] },
    } as WorkflowApiResponse;
    const applyGraph = vi.fn((): void => {
      throw new Error("Canvas unavailable");
    });
    const { result } = renderHook(() =>
      useWorkspaceCopilotUpdate({
        applyWorkflowUpdate: applyGraph,
      }),
    );
    await renderChat({ onWorkflowUpdate: result.current });
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Review changes", { updated_workflow: canonical }),
      );
      streamCalls[0]!.resolve();
    });
    cancelPost.mockResolvedValueOnce({ data: canonical });
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    applyGraph.mockImplementation(() => {});
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it.each(["user_cancelled", "copilot_recoverable_failure", null])(
    "reconciles the pre-turn canvas after recovered %s with unchanged canonical",
    async (terminal) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({
        onWorkflowUpdate: apply,
        onRestore: (snapshot) => {
          apply(
            {
              ...saveData.workflow,
              title: saveData.title,
              workflow_definition: {
                blocks: saveData.blocks,
                parameters: saveData.parameters,
              },
            },
            { settings: saveData.settings },
          );
          return restoreLive(snapshot);
        },
      });
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: saveData.workflow }
            : historyResponse,
        ),
      );
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
      });
      expect(apply).toHaveBeenCalledTimes(1);
      apply.mockClear();
      vi.useFakeTimers();
      cancelPost.mockRejectedValueOnce(new Error("Connection dropped"));
      await act(async () => {
        fireEvent.keyDown(textarea(), { key: "Escape" });
      });
      await act(async () => streamCalls[0]!.resolve());
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "New chat" })),
      );
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Turn ended",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: terminal,
          },
        },
      ];
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      if (terminal === null) expect(apply).not.toHaveBeenCalled();
      else
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          expect.objectContaining({
            title: saveData.title,
            workflow_definition: expect.objectContaining({
              blocks: saveData.blocks,
            }),
          }),
          expect.objectContaining({ settings: saveData.settings }),
        );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it.each([
    { action: "Retry", expired: true },
    { action: "Reload", expired: true },
    { action: "Reject", expired: true },
    { action: "Retry", expired: false },
    { action: "Reject", expired: false },
  ])(
    "settles an unannounced turn through $action (deadline expired: $expired)",
    async ({ action, expired }) => {
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      let canonical = saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await submit("edit the workflow");
      vi.useFakeTimers();
      await act(async () =>
        streamCalls[0]!.reject(new Error("Connection dropped")),
      );
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      if (expired) {
        await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
        expect(
          screen.getByText(/Could not confirm whether Copilot saved changes/),
        ).toBeTruthy();
        expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "New chat" })),
        );
        expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
        expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
          reservation,
        );
        const reads = historyGet.mock.calls.length;
        await act(async () => vi.advanceTimersByTimeAsync(60_000));
        expect(historyGet).toHaveBeenCalledTimes(reads);
      }
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(beginSaveTransaction(owner)).toBe(false);
      historyResponse.data.request_turn_id = "turn-1";
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Finished",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: action === "Reject" ? "cancelled" : null,
          },
        },
      ];
      if (action !== "Reject")
        canonical = {
          ...saveData.workflow,
          workflow_id: "wf_committed",
          version: 2,
        };
      historyGet.mockClear();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: action }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(historyGet).toHaveBeenCalledWith(
        "/workflow/copilot/chat-history",
        expect.objectContaining({
          params: expect.objectContaining({
            request_cancel_token: streamCalls[0]!.body.cancel_token,
          }),
        }),
      );
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.anything(),
      );
      if (action !== "Reject")
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      else
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          expect.objectContaining({
            cancel_token: streamCalls[0]!.body.cancel_token,
          }),
          expect.anything(),
        );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it("uses final correlated history and canonical reads at the deadline", async () => {
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    vi.useFakeTimers();
    const deadline = Date.now() + 1_500_000;
    const canonical = {
      ...saveData.workflow,
      workflow_id: "wf_committed",
      version: 2,
    };
    historyGet.mockImplementation((path: string) => {
      if (Date.now() >= deadline) {
        historyResponse.data.request_turn_id = "turn-1";
        historyResponse.data.chat_history = [
          {
            sender: "ai",
            content: "Saved",
            turn_outcome: {
              copilot_turn_id: "turn-1",
              terminal_reason: null,
            },
          },
        ];
      }
      return Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      );
    });
    await act(async () =>
      streamCalls[0]!.reject(new Error("Connection dropped")),
    );
    await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
    expect(apply).toHaveBeenCalledExactlyOnceWith(
      canonical,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("omits outgoing private settings while retaining raw values for rejection", async () => {
    changesState.hasChanges = true;
    saveData.settings.cdpConnectHeaders =
      '{"Authorization":"raw-auth","Cookie":"raw-cookie"}';
    saveData.settings.extraHttpHeaders = '{"X-Token":"raw-extra"}';
    const restore = vi.fn(restoreLive);
    await renderChat({ onRestore: restore });
    await submit("edit the workflow");
    const call = streamCalls[0]!;
    const document = parse(call.body.workflow_yaml);
    expect(document).not.toHaveProperty("cdp_connect_headers");
    expect(document).not.toHaveProperty("extra_http_headers");
    expect.soft(document).not.toHaveProperty("totp_identifier");
    expect.soft(document).not.toHaveProperty("totp_verification_url");
    expect(call.body.workflow_yaml).not.toContain("raw-");
    await act(async () => {
      call.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      call.onMessage(proposalResponse("Draft ready."));
      call.resolve();
    });
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(restore).toHaveBeenCalledOnce();
    expect(editorNodes[0]?.data).toMatchObject({
      cdpConnectHeaders: '{"Authorization":"raw-auth","Cookie":"raw-cookie"}',
      extraHttpHeaders: '{"X-Token":"raw-extra"}',
      totpIdentifier: saveData.settings.totpIdentifier,
      totpVerificationUrl: saveData.settings.totpVerificationUrl,
    });
  });

  it.each([false, true])(
    "restores headers and keeps a persisted terminal apply dirty (unsaved headers: %s)",
    async (unsaved) => {
      const headers = {
        totp_identifier: unsaved ? "local-totp" : "stored-totp",
        totp_verification_url: `https://example.test/totp?token=${unsaved ? "local" : "stored"}`,
        cdp_connect_headers: {
          Authorization: unsaved ? "local-cdp" : "stored-cdp",
        },
        extra_http_headers: {
          "X-Token": unsaved ? "local-extra" : "stored-extra",
        },
      };
      saveData.workflow = {
        ...saveData.workflow,
        totp_identifier: "stored-totp",
        totp_verification_url: "https://example.test/totp?token=stored",
        cdp_connect_headers: { Authorization: "stored-cdp" },
        extra_http_headers: { "X-Token": "stored-extra" },
      };
      saveData.settings.cdpConnectHeaders = JSON.stringify(
        headers.cdp_connect_headers,
      );
      saveData.settings.extraHttpHeaders = JSON.stringify(
        headers.extra_http_headers,
      );
      saveData.settings.totpIdentifier = headers.totp_identifier;
      saveData.settings.totpVerificationUrl = headers.totp_verification_url;
      changesState.hasChanges = unsaved;
      const snapshot = structuredClone(saveData.settings);
      let editorSettings = snapshot;
      const apply = vi.fn(
        (workflow: WorkflowApiResponse, options?: WorkflowUpdateOptions) => {
          editorSettings = options?.settings ?? apiWorkflowToSettings(workflow);
          saveData.settings = editorSettings;
          changesState.setHasChanges(!options?.persisted);
        },
      );
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      const call = streamCalls[0]!;
      await act(async () => {
        call.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        call.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload({
            extra_http_headers: null,
            cdp_connect_headers: null,
          }),
        });
        call.onMessage(
          proposalResponse("Applied.", {
            updated_workflow: proposedWorkflowPayload({
              extra_http_headers: null,
              cdp_connect_headers: { Authorization: "***" },
              max_elapsed_time_minutes: 40,
            }) as unknown as WorkflowApiResponse,
            proposal_disposition: "auto_applicable",
            workflow_applied: true,
          }),
        );
        call.resolve();
      });
      expect(editorSettings).toMatchObject({
        cdpConnectHeaders: snapshot.cdpConnectHeaders,
        extraHttpHeaders: snapshot.extraHttpHeaders,
        totpIdentifier: snapshot.totpIdentifier,
        totpVerificationUrl: snapshot.totpVerificationUrl,
        maxElapsedTimeMinutes: 40,
      });
      expect(apply).toHaveBeenLastCalledWith(
        expect.anything(),
        expect.objectContaining({ persisted: true }),
      );
      expect(changesState.setHasChanges).toHaveBeenLastCalledWith(true);
      changesState.setHasChanges.mockClear();
      await submit("edit again with preserved headers");
      await act(async () => {
        streamCalls[1]!.onMessage(
          proposalResponse("Applied again.", {
            updated_workflow: proposedWorkflowPayload(
              headers,
            ) as unknown as WorkflowApiResponse,
            proposal_disposition: "auto_applicable",
            workflow_applied: true,
          }),
        );
        streamCalls[1]!.resolve();
      });
      expect(editorSettings).toMatchObject({
        cdpConnectHeaders: snapshot.cdpConnectHeaders,
        extraHttpHeaders: snapshot.extraHttpHeaders,
        totpIdentifier: snapshot.totpIdentifier,
        totpVerificationUrl: snapshot.totpVerificationUrl,
      });
      expect(changesState.setHasChanges).toHaveBeenLastCalledWith(false);
    },
  );

  it.each([
    ["totpIdentifier", "totp_identifier", "local-totp"],
    [
      "totpVerificationUrl",
      "totp_verification_url",
      "https://example.test/totp?token=local",
    ],
  ] as const)(
    "marks a persisted apply dirty when only %s was dropped",
    async (setting, field, value) => {
      saveData.settings.cdpConnectHeaders = null;
      saveData.settings.extraHttpHeaders = null;
      saveData.settings.totpIdentifier = null;
      saveData.settings.totpVerificationUrl = null;
      saveData.settings[setting] = value;
      let editorSettings = structuredClone(saveData.settings);
      await renderChat({
        onWorkflowUpdate: (workflow, options) => {
          editorSettings = options?.settings ?? apiWorkflowToSettings(workflow);
          saveData.settings = editorSettings;
          changesState.setHasChanges(!options?.persisted);
        },
      });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage(
          proposalResponse("Applied.", {
            updated_workflow: proposedWorkflowPayload({
              [field]: null,
            }) as unknown as WorkflowApiResponse,
            proposal_disposition: "auto_applicable",
            workflow_applied: true,
          }),
        );
        streamCalls[0]!.resolve();
      });
      expect.soft(editorSettings[setting]).toBe(value);
      expect.soft(changesState.setHasChanges).toHaveBeenLastCalledWith(true);
      changesState.setHasChanges.mockClear();
      await submit("edit again");
      await act(async () => {
        streamCalls[1]!.onMessage(
          proposalResponse("Applied again.", {
            updated_workflow: proposedWorkflowPayload({
              [field]: value,
            }) as unknown as WorkflowApiResponse,
            proposal_disposition: "auto_applicable",
            workflow_applied: true,
          }),
        );
        streamCalls[1]!.resolve();
      });
      expect.soft(editorSettings[setting]).toBe(value);
      expect.soft(changesState.setHasChanges).toHaveBeenLastCalledWith(false);
    },
  );

  it("keeps live headers when a streamed draft omits both header keys", async () => {
    saveData.settings.cdpConnectHeaders = '{"Authorization":"draft-cdp"}';
    saveData.settings.extraHttpHeaders = '{"X-Token":"draft-extra"}';
    const snapshot = structuredClone(saveData.settings);
    let editorSettings = snapshot;
    await renderChat({
      onWorkflowUpdate: (workflow, options) => {
        editorSettings = options?.settings ?? apiWorkflowToSettings(workflow);
      },
    });
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload({ max_elapsed_time_minutes: 40 }),
      });
    });
    expect(editorSettings).toMatchObject({
      cdpConnectHeaders: snapshot.cdpConnectHeaders,
      extraHttpHeaders: snapshot.extraHttpHeaders,
      totpIdentifier: snapshot.totpIdentifier,
      totpVerificationUrl: snapshot.totpVerificationUrl,
      maxElapsedTimeMinutes: 40,
    });
    await act(async () => {
      streamCalls[0]!.onMessage(plainReplyResponse("Done."));
      streamCalls[0]!.resolve();
    });
  });

  it.each(["server", "missing-chat", "failed-server"])(
    "retains private headers across the %s Accept path",
    async (mode) => {
      saveData.settings.cdpConnectHeaders = '{"Authorization":"accept-cdp"}';
      saveData.settings.extraHttpHeaders = '{"X-Token":"accept-extra"}';
      const snapshot = structuredClone(saveData.settings);
      let editorSettings = snapshot;
      await renderChat({
        onWorkflowUpdate: (workflow, options) => {
          editorSettings = options?.settings ?? apiWorkflowToSettings(workflow);
          saveData.settings = editorSettings;
          changesState.setHasChanges(!options?.persisted);
        },
      });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage(
          proposalResponse("Draft ready.", {
            workflow_copilot_chat_id: mode === "missing-chat" ? "" : "chat-1",
          }),
        );
        streamCalls[0]!.resolve();
      });
      if (mode === "missing-chat") {
        historyResponse.data.workflow_copilot_chat_id = null;
      } else if (mode === "failed-server") {
        historyResponse.data.proposed_workflow = proposedWorkflowPayload();
        cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
      } else {
        cancelPost.mockResolvedValueOnce({
          data: proposedWorkflowPayload({
            extra_http_headers: null,
            cdp_connect_headers: { Authorization: "***" },
          }),
        });
      }
      changesState.setHasChanges.mockClear();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Accept" })),
      );
      expect(editorSettings).toMatchObject({
        cdpConnectHeaders: snapshot.cdpConnectHeaders,
        extraHttpHeaders: snapshot.extraHttpHeaders,
        totpIdentifier: snapshot.totpIdentifier,
        totpVerificationUrl: snapshot.totpVerificationUrl,
      });
      if (mode === "server")
        expect(changesState.setHasChanges).toHaveBeenLastCalledWith(true);
      else {
        expect(changesState.setHasChanges).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance === null,
        ).toBe(mode === "missing-chat");
      }
    },
  );

  it.each([false, true])(
    "keeps a header edited after the proposal when Accept returns the older saved header (buffered: %s)",
    async (buffered) => {
      saveData.settings.extraHttpHeaders = '{"X-Token":"before-proposal"}';
      const savedWorkflow = proposedWorkflowPayload({
        extra_http_headers: { "X-Token": "before-proposal" },
        cdp_connect_headers: { Authorization: "********" },
        totp_identifier: saveData.settings.totpIdentifier,
        totp_verification_url: saveData.settings.totpVerificationUrl,
      });
      await renderChat({
        onWorkflowUpdate: (workflow, options) => {
          saveData.settings =
            options?.settings ?? apiWorkflowToSettings(workflow);
          changesState.setHasChanges(!options?.persisted);
        },
      });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      const editedSettings = {
        ...saveData.settings,
        extraHttpHeaders: '{"X-Token":"after-proposal"}',
      };
      if (buffered) {
        useWorkflowYamlEditorStore.setState({
          flushDraft: () => {
            useWorkflowHasChangesStore.setState({
              getSaveData: () => ({ ...saveData, settings: editedSettings }),
            });
          },
        });
      } else saveData.settings = editedSettings;
      cancelPost.mockResolvedValueOnce({ data: savedWorkflow });

      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Accept" })),
      );

      expect(saveData.settings.extraHttpHeaders).toBe(
        '{"X-Token":"after-proposal"}',
      );
      expect(changesState.setHasChanges).toHaveBeenLastCalledWith(true);
    },
  );

  it("sends with malformed local headers and restores their original text on rejection", async () => {
    changesState.hasChanges = true;
    saveData.settings.cdpConnectHeaders = '{"Authorization":';
    const restore = vi.fn(restoreLive);
    await renderChat({ onRestore: restore });
    await submit("edit the workflow");
    expect(streamCalls).toHaveLength(1);
    expect(
      parse(streamCalls[0]!.body.workflow_yaml).cdp_connect_headers,
    ).toBeUndefined();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(restore).toHaveBeenCalledOnce();
    expect(editorNodes[0]?.data).toMatchObject({
      cdpConnectHeaders: '{"Authorization":',
    });
  });

  it("refuses edits and YAML commits during streaming, permits owner apply and edits after terminal", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn(() => {
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      useWorkflowTitleStore.getState().setTitle("Owned title");
      expect(useWorkflowTitleStore.getState().title).toBe("Owned title");
    });
    useWorkflowYamlEditorStore.getState().open("blocks: []");
    useWorkflowYamlEditorStore
      .getState()
      .setDraft("blocks: []\ntitle: Retained draft");
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    const commit = vi.fn().mockResolvedValue(true);
    useWorkflowYamlEditorStore.getState().registerCommit(commit);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    const parameter = {
      key: "streaming_input",
      parameterType: "context" as const,
      sourceParameterKey: "source",
    };
    act(() => {
      useWorkflowParametersStore.getState().setParameters([parameter]);
      useWorkflowTitleStore.getState().setTitle("Edited during streaming");
      useWorkflowYamlEditorStore
        .getState()
        .setDraft("blocks: []\ntitle: Lost draft");
    });
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      {
        parameterType: "context",
        key: "context",
        sourceParameterKey: "unsaved_source",
      },
    ]);
    expect(useWorkflowTitleStore.getState().title).toBe(saveData.title);
    expect(useWorkflowYamlEditorStore.getState().draft).toBe(
      "blocks: []\ntitle: Retained draft",
    );
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Wait for the Copilot change to finish",
      }),
    );
    expect(await commitYamlDraft(true)).toBe(false);
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().error).toBe(
      "Wait for the Copilot change to finish",
    );
    expect(commit).not.toHaveBeenCalled();
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(apply).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        persisted: true,
        applied: true,
      }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(useWorkflowTitleStore.getState().title).toBe(
      proposedWorkflowPayload().title,
    );
    act(() => {
      useWorkflowParametersStore.getState().setParameters([parameter]);
      useWorkflowTitleStore.getState().setTitle("After terminal");
    });
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      parameter,
    ]);
    expect(useWorkflowTitleStore.getState().title).toBe("After terminal");
    expect(await commitYamlDraft(true)).toBe(false);
    act(() => useWorkflowYamlEditorStore.getState().open("blocks: []"));
    expect(await commitYamlDraft(true)).toBe(true);
  });

  it.each([false, true])(
    "retains speech and the prompt when a reservation blocks sending (listening: %s)",
    async (listening) => {
      speech.isListening = listening;
      speech.takeAudioBlob.mockReturnValue(
        new Blob(["audio"], { type: "audio/webm" }),
      );
      await renderChat();
      const token = beginCopilotAcceptance()!;
      await submit("edit with speech");
      expect(postStreaming).not.toHaveBeenCalled();
      expect(textarea().value).toBe("edit with speech");
      expect(speech.stop).not.toHaveBeenCalled();
      expect(speech.takeAudioBlob).not.toHaveBeenCalled();
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Wait for the Copilot change to finish",
        }),
      );
      finishCopilotAcceptance(token);
    },
  );

  it("refuses to send during a YAML commit without consuming the prompt", async () => {
    await renderChat();
    const owner = createYamlCommitOwner("wpid_1");
    expect(beginYamlCommit(owner)).toBe(true);
    await submit("edit the workflow");
    expect(postStreaming).not.toHaveBeenCalled();
    expect(textarea().value).toBe("edit the workflow");
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "A YAML commit is in progress" }),
    );
    finishYamlCommit(owner);
    await submit("edit the workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("retries the confirmed terminal workflow after YAML unlock and retains the draft", async () => {
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const canonical = {
      ...saveData.workflow,
      title: "Canonical Copilot change",
      version: 2,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await submit("edit the workflow");
    act(() => {
      useWorkflowYamlEditorStore.getState().open("title: Original");
      useWorkflowYamlEditorStore.setState({
        draft: "title: My YAML draft",
        commitInProgress: true,
      });
    });
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          updated_workflow: canonical,
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(apply).not.toHaveBeenCalled();
    expect(historyGet).not.toHaveBeenCalledWith(
      "/workflows/wpid_1",
      expect.anything(),
    );
    await act(async () => {
      useWorkflowYamlEditorStore.getState().close();
      useWorkflowYamlEditorStore.getState().setCommitInProgress(false);
    });
    await waitFor(() =>
      expect(apply).toHaveBeenCalledWith(
        canonical,
        expect.objectContaining({
          persisted: true,
          applied: true,
          settings: expect.objectContaining({
            cdpConnectHeaders: saveData.settings.cdpConnectHeaders,
            extraHttpHeaders: saveData.settings.extraHttpHeaders,
            totpIdentifier: saveData.settings.totpIdentifier,
            totpVerificationUrl: saveData.settings.totpVerificationUrl,
          }),
        }),
      ),
    );
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      active: true,
      draft: "title: My YAML draft",
      entrySnapshot: "title: Original",
      error:
        "The workflow changed while YAML was open. Reopen the YAML view to continue.",
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it.each([
    "network error",
    "malformed frame",
    "SSE stream ended without terminal event",
  ])(
    "reconciles unknown persistence after %s without rolling back staged work",
    async (failure) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
      });
      apply.mockClear();
      let resolveRead!: (value: unknown) => void;
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? new Promise((resolve) => {
              resolveRead = resolve;
            })
          : Promise.resolve(historyResponse),
      );
      vi.useFakeTimers();
      await act(async () => streamCalls[0]!.reject(new Error(failure)));
      expect(resolveRead).toBeTypeOf("function");
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
      const canonical = {
        ...saveData.workflow,
        workflow_id: "wf_committed",
        version: 2,
      };
      await act(async () => resolveRead({ data: canonical }));
      expect(apply).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Saved the workflow.",
          created_at: "2026-09-11T00:00:00Z",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: null,
          },
        },
      ];
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({
          persisted: true,
          applied: true,
          settings: expect.objectContaining({
            cdpConnectHeaders: saveData.settings.cdpConnectHeaders,
            extraHttpHeaders: saveData.settings.extraHttpHeaders,
            totpIdentifier: saveData.settings.totpIdentifier,
            totpVerificationUrl: saveData.settings.totpVerificationUrl,
          }),
        }),
      );
      expect(
        useWorkflowHasChangesStore.getState().setHasChanges,
      ).toHaveBeenLastCalledWith(true);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it.each(["late commit", "unchanged", "unconfirmed commit"])(
    "keeps pre-turn failure reconciliation locked until %s settles",
    async (outcome) => {
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const committedWorkflow = {
        ...saveData.workflow,
        workflow_id: "wf_committed",
        version: 2,
      };
      let canonical =
        outcome === "unconfirmed commit"
          ? committedWorkflow
          : saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await submit("edit the workflow");
      vi.useFakeTimers();
      await act(async () => streamCalls[0]!.reject(new Error("network error")));
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.anything(),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
      if (outcome !== "unchanged") {
        canonical = committedWorkflow;
        historyResponse.data.request_turn_id = "turn-1";
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(historyGet).toHaveBeenCalledWith(
          "/workflow/copilot/chat-history",
          expect.objectContaining({
            params: expect.objectContaining({
              request_cancel_token: streamCalls[0]!.body.cancel_token,
            }),
          }),
        );
        expect(apply).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
      }
      if (outcome === "late commit") {
        historyResponse.data.chat_history = [
          {
            sender: "ai",
            content: "Changes saved.",
            created_at: new Date().toISOString(),
            turn_outcome: {
              copilot_turn_id: "turn-1",
              terminal_reason: null,
            },
          },
        ];
        await act(async () => vi.advanceTimersByTimeAsync(3_000));
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      } else {
        await act(async () =>
          vi.advanceTimersByTimeAsync(
            outcome === "unchanged" ? 1_499_999 : 1_497_999,
          ),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        await act(async () => vi.advanceTimersByTimeAsync(1));
        expect(apply).not.toHaveBeenCalled();
        expect(
          screen.getByText(/Could not confirm whether Copilot saved changes/),
        ).toBeTruthy();
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
        expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
        return;
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      const nextCommit = createYamlCommitOwner("wpid_1");
      expect(beginYamlCommit(nextCommit)).toBe(true);
      finishYamlCommit(nextCommit);
    },
  );

  it.each([
    ["error", true],
    ["error", false],
    ["response", true],
    ["response", false],
    ["cancel", true],
    ["cancel", false],
  ] as const)(
    "checks canonical before a %s snapshot restore (auto-accept committed: %s)",
    async (terminal, committed) => {
      changesState.hasChanges = true;
      historyResponse.data.auto_accept = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
      });
      apply.mockClear();
      let resolveRead!: (value: unknown) => void;
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? new Promise((resolve) => {
              resolveRead = resolve;
            })
          : Promise.resolve(historyResponse),
      );
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage(
          terminal === "error"
            ? { type: "error", turn_id: "turn-1", error: "Reply write failed" }
            : plainReplyResponse("Turn ended.", {
                turn_id: "turn-1",
                cancelled: terminal === "cancel",
                narrative_payload: { terminal: "error", turnId: "turn-1" },
              }),
        );
        streamCalls[0]!.resolve();
      });
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);

      const canonical = committed
        ? {
            ...saveData.workflow,
            workflow_id: "wf_committed",
            title: "Auto-accepted workflow",
            version: 2,
            browser_type: "chrome",
            workflow_definition: { parameters: [], blocks: [] },
          }
        : saveData.workflow;
      await act(async () => resolveRead({ data: canonical }));
      expect(apply).not.toHaveBeenCalled();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Turn finished",
          turn_outcome: { copilot_turn_id: "turn-1", terminal_reason: "error" },
        },
      ];
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      if (committed) {
        expect(apply).toHaveBeenCalledTimes(1);
        expect(setEditorNodes).not.toHaveBeenCalled();
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({
            persisted: true,
            applied: true,
            settings: expect.objectContaining({ browserType: "chrome" }),
          }),
        );
      } else {
        expect(apply).not.toHaveBeenCalled();
        expect(setEditorNodes).toHaveBeenCalledOnce();
        expect(
          editorNodes.find((node) => node.id === "loop")?.data,
        ).toMatchObject({
          loopValue: "unsaved_items",
          loopVariableReference: "{{ item }}",
        });
        expect(editorNodes[0]?.data).toMatchObject(saveData.settings);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
        expect(useWorkflowTitleStore.getState()).toMatchObject({
          title: saveData.title,
          description: saveData.description,
        });
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it("waits for the errored turn's own history row before applying a late commit", async () => {
    historyResponse.data.auto_accept = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    let canonical = saveData.workflow;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
    });
    apply.mockClear();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "error",
        turn_id: "turn-1",
        error: "Finalizer still running",
      });
      streamCalls[0]!.resolve();
    });
    expect(apply).not.toHaveBeenCalled();
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    expect(
      screen.getByRole("button", { name: "Retry" }).getAttribute("disabled"),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Reject" }).getAttribute("disabled"),
    ).toBeNull();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Other turn",
        turn_outcome: {
          copilot_turn_id: "turn-other",
          terminal_reason: null,
        },
      },
    ];
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apply).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.objectContaining({ source: "stop_button" }),
      expect.anything(),
    );
    expect(apply).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    canonical = {
      ...saveData.workflow,
      workflow_id: "wf_late_commit",
      version: 2,
      workflow_definition: { parameters: [], blocks: [] },
    };
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Turn interrupted",
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "interrupted",
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(apply).not.toHaveBeenCalled();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Final commit saved",
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: null,
        },
      },
    ];
    historyGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
      "/workflow/copilot/chat-history",
      "/workflows/wpid_1",
    ]);
    expect(apply).toHaveBeenCalledExactlyOnceWith(
      canonical,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it.each(["response", "failure", "unchanged canonical"])(
    "retries a settled recovery tick immediately after a history %s",
    async (outcome) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({
        onWorkflowUpdate: apply,
        onRestore: (snapshot) => {
          apply(
            {
              ...saveData.workflow,
              title: saveData.title,
              workflow_definition: {
                blocks: saveData.blocks,
                parameters: saveData.parameters,
              },
            },
            { settings: saveData.settings },
          );
          return restoreLive(snapshot);
        },
      });
      await submit("edit the workflow");
      let canonical = saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: "turn-1",
          error: "Finalizer still running",
        });
        streamCalls[0]!.resolve();
      });
      apply.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(50_000));
      if (outcome === "failure") {
        historyGet.mockRejectedValueOnce(new Error("offline"));
        await act(async () => vi.advanceTimersByTimeAsync(30_000));
      }
      expect(apply).not.toHaveBeenCalled();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Final commit saved",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: null,
          },
        },
      ];
      if (outcome === "unchanged canonical") {
        // Unchanged canonical normally restores the snapshot and ends recovery.
        // A failed editor restore keeps this settled read pending for Retry.
        apply.mockImplementationOnce(() => {
          throw new Error("Editor could not restore the snapshot");
        });
        historyGet.mockClear();
        await act(async () => vi.advanceTimersByTimeAsync(30_000));
        expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
          "/workflow/copilot/chat-history",
          "/workflows/wpid_1",
        ]);
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          expect.objectContaining({ title: saveData.title }),
          expect.objectContaining({ settings: saveData.settings }),
        );
        apply.mockClear();
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      historyGet.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(historyGet).not.toHaveBeenCalled();
      canonical = {
        ...saveData.workflow,
        workflow_id: "wf_late_commit",
        version: 2,
      };
      const retryTime = Date.now();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(Date.now()).toBe(retryTime);
      expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
        "/workflow/copilot/chat-history",
        "/workflows/wpid_1",
      ]);
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true, applied: true }),
      );
    },
  );

  it.each(["Retry", "Reject", "timeout"])(
    "starts a fresh recovery tick after %s interrupts a canonical read",
    async (action) => {
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: saveData.workflow }
            : historyResponse,
        ),
      );
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: "turn-1",
          error: "Finalizer still running",
        });
        streamCalls[0]!.resolve();
      });
      apply.mockClear();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Final commit saved",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: null,
          },
        },
      ];
      let readSignal: AbortSignal | undefined;
      let resolveRead!: (value: unknown) => void;
      historyGet.mockImplementation(
        (path: string, options?: { signal?: AbortSignal }) => {
          if (path !== "/workflows/wpid_1")
            return Promise.resolve(historyResponse);
          readSignal = options?.signal;
          return new Promise((resolve) => {
            resolveRead = resolve;
          });
        },
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(readSignal?.aborted).toBe(false);
      expect(historyGet).toHaveBeenLastCalledWith(
        "/workflows/wpid_1",
        expect.objectContaining({ timeout: 5_000, signal: readSignal }),
      );
      expect(apply).not.toHaveBeenCalled();
      if (action === "timeout") {
        await act(async () => vi.advanceTimersByTimeAsync(5_000));
        expect(readSignal?.aborted).toBe(true);
      }
      const canonical = {
        ...saveData.workflow,
        workflow_id: "wf_late_commit",
        version: 2,
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      historyGet.mockClear();
      const retryTime = Date.now();
      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", {
            name: action === "timeout" ? "Retry" : action,
          }),
        );
      });
      expect(readSignal?.aborted).toBe(true);
      if (action === "Reject") {
        expect(cancelPost).toHaveBeenCalledExactlyOnceWith(
          "/workflow/copilot/cancel",
          expect.objectContaining({ source: "stop_button" }),
          { timeout: 5_000 },
        );
      }
      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(Date.now()).toBe(retryTime);
      expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
        "/workflow/copilot/chat-history",
        "/workflows/wpid_1",
      ]);
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true, applied: true }),
      );
      await act(async () => resolveRead({ data: saveData.workflow }));
      expect(apply).toHaveBeenCalledTimes(1);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it("bounds a hung terminal canonical read and hands cleanup to the recovery poll", async () => {
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    const signals: AbortSignal[] = [];
    historyGet.mockImplementation(
      (path: string, options?: { signal?: AbortSignal }) => {
        if (path !== "/workflows/wpid_1")
          return Promise.resolve(historyResponse);
        if (options?.signal) signals.push(options.signal);
        return new Promise(() => {});
      },
    );
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
      streamCalls[0]!.onMessage({
        type: "error",
        turn_id: "turn-1",
        error: "Reply write failed",
      });
      streamCalls[0]!.resolve();
    });
    apply.mockClear();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(signals[0]?.aborted).toBe(true);
    expect(
      screen.getByRole("button", { name: "Send" }).getAttribute("disabled"),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Retry" }).getAttribute("disabled"),
    ).toBeNull();
    historyGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(historyGet).toHaveBeenCalledWith(
      "/workflow/copilot/chat-history",
      expect.anything(),
    );
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    await act(async () => vi.advanceTimersByTimeAsync(1_503_000));
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(apply).not.toHaveBeenCalled();
    historyGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(historyGet).not.toHaveBeenCalled();
  });

  it("keeps staged work when a failed transport finds unchanged canonical state", async () => {
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
    });
    apply.mockClear();
    await act(async () => streamCalls[0]!.reject(new Error("network error")));
    expect(historyGet).toHaveBeenCalledWith(
      "/workflows/wpid_1",
      expect.anything(),
    );
    expect(apply).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("retains a queued prompt when the browser connects during manual Accept", async () => {
    const view = await renderChat();
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    view.rerender(
      <WorkflowCopilotChat requiresLiveBrowser isLiveBrowserReady={false} />,
    );
    await submit("build another block");
    expect(streamCalls).toHaveLength(1);
    let resolveAccept!: (value: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAccept = resolve;
        }),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.anything(),
        { timeout: 30_000, signal: expect.any(AbortSignal) },
      ),
    );
    view.rerender(
      <WorkflowCopilotChat
        requiresLiveBrowser
        isLiveBrowserReady
        liveBrowserSessionId="pbs_ready"
      />,
    );
    expect(streamCalls).toHaveLength(1);
    await act(async () => resolveAccept({ data: proposedWorkflowPayload() }));
    await waitFor(() => expect(streamCalls).toHaveLength(2));
    expect(streamCalls[1]!.body.message).toBe("build another block");
    await act(async () => {
      streamCalls[1]!.onMessage(plainReplyResponse("Done."));
      streamCalls[1]!.resolve();
    });
    expect(streamCalls).toHaveLength(2);
  });

  it.each(["terminal", "manual", "fallback", "canonical"])(
    "preserves YAML settings through the %s path",
    async (mode) => {
      const appliedWorkflow = {
        ...saveData.workflow,
        workflow_id: "wf_accepted",
        version: 2,
        title: "Accepted title",
        description: "Accepted description",
        workflow_definition: { version: 2, parameters: [], blocks: [] },
        enable_self_healing: true,
        mask_secrets: true,
      } as WorkflowApiResponse;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      act(() => useWorkflowYamlEditorStore.getState().open("title: Original"));
      await submit("edit the workflow");
      if (mode === "canonical") {
        historyGet.mockImplementation((path: string) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: appliedWorkflow }
              : historyResponse,
          ),
        );
        vi.useFakeTimers();
        await act(async () =>
          streamCalls[0]!.reject(new Error("network error")),
        );
        expect(apply).not.toHaveBeenCalled();
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(
          "title: Original",
        );
        historyResponse.data.request_turn_id = "turn-1";
        historyResponse.data.chat_history = [
          {
            sender: "ai",
            content: "Changes saved.",
            created_at: new Date().toISOString(),
            turn_outcome: {
              copilot_turn_id: "turn-1",
              terminal_reason: null,
            },
          },
        ];
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(historyGet).toHaveBeenCalledWith(
          "/workflow/copilot/chat-history",
          expect.objectContaining({
            params: expect.objectContaining({
              request_cancel_token: streamCalls[0]!.body.cancel_token,
            }),
          }),
        );
        expect(apply).toHaveBeenCalled();
      } else {
        await act(async () => {
          streamCalls[0]!.onMessage(
            proposalResponse("Ready.", {
              updated_workflow: appliedWorkflow,
              ...(mode === "terminal"
                ? {
                    proposal_disposition: "auto_applicable",
                    workflow_applied: true,
                  }
                : {}),
            }),
          );
          streamCalls[0]!.resolve();
        });
        if (mode !== "terminal") {
          if (mode === "fallback") {
            historyResponse.data.proposed_workflow = appliedWorkflow;
            cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
          } else cancelPost.mockResolvedValueOnce({ data: appliedWorkflow });
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "Accept" })),
          );
        }
      }
      const state = useWorkflowYamlEditorStore.getState();
      expect(state.active).toBe(true);
      if (mode !== "fallback") expect(state.error).toBeNull();
      if (mode === "fallback") {
        expect(parse(state.draft)).toEqual({ title: "Original" });
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        return;
      }
      expect(parse(state.draft)).toMatchObject({
        title: "Accepted title",
        description: "Accepted description",
        mask_secrets: true,
        cdp_connect_headers: { Authorization: "********" },
        totp_identifier: saveData.settings.totpIdentifier,
        workflow_definition: { version: 2, blocks: [] },
      });
      expect(state.entrySnapshot).toBe(state.draft);
    },
  );

  it("refreshes YAML with the persisted title when no title edit was recorded", async () => {
    useWorkflowTitleStore.getState().setTitle("Current custom title");
    await renderChat({
      onWorkflowUpdate: (workflow) =>
        useWorkflowTitleStore.getState().syncTitleFromWorkflow(workflow.title),
    });
    act(() =>
      useWorkflowYamlEditorStore.getState().open("title: Current custom title"),
    );
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          updated_workflow: {
            ...saveData.workflow,
            title: "New Workflow",
            workflow_definition: { version: 2, parameters: [], blocks: [] },
          },
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(useWorkflowTitleStore.getState().title).toBe("New Workflow");
    expect(parse(useWorkflowYamlEditorStore.getState().draft).title).toBe(
      "New Workflow",
    );
  });

  it("retains edited YAML and refuses both commit paths until the view is reopened", async () => {
    await renderChat({ onWorkflowUpdate: vi.fn() });
    const store = useWorkflowYamlEditorStore.getState();
    act(() => {
      store.open("title: Original");
      store.setDraft("title: Edited");
    });
    const commit = vi.fn().mockResolvedValue(true);
    store.registerCommit(commit);
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(useWorkflowYamlEditorStore.getState().draft).toBe("title: Edited");
    expect(useWorkflowYamlEditorStore.getState().entrySnapshot).toBe(
      "title: Original",
    );
    await act(async () => {
      expect(await commitYamlDraft(true)).toBe(false);
      expect(await commitYamlDraft(false)).toBe(false);
    });
    expect(commit).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title:
          "The workflow changed while YAML was open. Reopen the YAML view to continue.",
      }),
    );
    act(() => store.setDraft("title: Original"));
    await act(async () => expect(await commitYamlDraft(false)).toBe(false));
    act(() =>
      store.open(
        stringify({
          title: "Accepted title",
          workflow_definition: { blocks: [] },
        }),
      ),
    );
    await act(async () => expect(await commitYamlDraft(false)).toBe(true));
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it("blocks YAML commits until a pending Copilot acceptance finishes", async () => {
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    apply.mockClear();
    cancelPost.mockClear();
    const competingAcceptance = beginCopilotAcceptance()!;
    vi.mocked(toast).mockClear();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    expect(cancelPost).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Wait for the Copilot change to finish",
      }),
    );
    finishCopilotAcceptance(competingAcceptance);
    let resolveAccept!: (response: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAccept = resolve;
        }),
    );
    const commit = vi.fn().mockResolvedValue(true);
    useWorkflowYamlEditorStore.getState().registerCommit(commit);
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.anything(),
        { timeout: 30_000, signal: expect.any(AbortSignal) },
      ),
    );
    await act(async () => {
      expect(await commitYamlDraft(true)).toBe(false);
      expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    });
    expect(commit).not.toHaveBeenCalled();
    expect(useWorkflowYamlEditorStore.getState().error).toBe(
      "Wait for the Copilot change to finish",
    );
    expect(apply).not.toHaveBeenCalled();
    await act(async () => resolveAccept({ data: proposedWorkflowPayload() }));
    expect(apply).toHaveBeenCalledTimes(1);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await act(async () => expect(await commitYamlDraft(true)).toBe(true));
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it.each(["in flight", "uncertain", "409", "409 unreadable refresh"])(
    "parks Accept across remount: %s",
    async (outcome) => {
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-accept",
        revision: 1,
        canonical_fingerprint: "before",
        disposition: "review_untested",
        workflow_run_id: null,
      };
      const apply = vi.fn();
      const view = await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      let resolveAccept!: (value: unknown) => void;
      cancelPost.mockImplementationOnce(() =>
        outcome === "in flight"
          ? new Promise((resolve) => {
              resolveAccept = resolve;
            })
          : Promise.reject(
              outcome.startsWith("409")
                ? {
                    response: {
                      status: 409,
                      ...(outcome === "409 unreadable refresh"
                        ? {
                            data: {
                              detail:
                                "Copilot proposal is already being accepted",
                            },
                          }
                        : {}),
                    },
                  }
                : new Error("lost response"),
            ),
      );
      if (outcome === "409")
        historyResponse.data.proposed_claim_expires_in_seconds = 120;
      if (outcome === "409 unreadable refresh")
        historyGet.mockRejectedValue(new Error("history unavailable"));
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      historyResponse.data.proposed_workflow_metadata = {
        ...historyResponse.data.proposed_workflow_metadata!,
        disposition: "accepting",
        claimed_at: new Date().toISOString(),
      };
      view.unmount();
      expect(beginSaveTransaction(owner)).toBe(false);
      unregisterEditorOwner(owner);
      const otherOwner = createYamlCommitOwner("wpid-other");
      registerEditorOwner(otherOwner);
      expect(beginSaveTransaction(otherOwner)).toBe(true);
      unregisterEditorOwner(otherOwner);
      const returnedOwner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(returnedOwner);
      expect(useWorkflowYamlEditorStore.getState().lockKind).toBe("copilot");
      expect(beginSaveTransaction(returnedOwner)).toBe(false);
      const remountedApply = vi.fn();
      render(<WorkflowCopilotChat onWorkflowUpdate={remountedApply} />);
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      expect(useWorkflowYamlEditorStore.getState().lockKind).toBe("copilot");
      finishSaveTransaction(otherOwner);
      expect(beginSaveTransaction(owner)).toBe(false);
      const canonical = {
        ...saveData.workflow,
        workflow_id: "wf_accepted",
        version: 2,
      };
      if (outcome === "in flight")
        await act(async () => resolveAccept({ data: canonical }));
      expect(apply).not.toHaveBeenCalled();
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(remountedApply).toHaveBeenCalledWith(
        canonical,
        expect.objectContaining({ persisted: true }),
      );
      expect(beginSaveTransaction(returnedOwner)).toBe(true);
    },
  );

  it.each([
    "missing",
    "stored",
    "created during request",
    "cleared during request",
  ])(
    "uses the history request's recovery token for unanswered-turn fallback: %s",
    async (tokenState) => {
      const key = "copilot-credential-recovery:wpid_1";
      const hadToken =
        tokenState === "stored" || tokenState === "cleared during request";
      if (hadToken) sessionStorage.setItem(key, "original-capability");
      historyResponse.data.chat_history = [
        {
          sender: "user",
          content: "Sign in",
          turn_id: "turn-paused",
          created_at: new Date().toISOString(),
        },
      ];
      let resolveHistory!: (response: typeof historyResponse) => void;
      historyGet.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveHistory = resolve;
          }),
      );
      await renderChat();
      await waitFor(() => expect(historyGet).toHaveBeenCalledTimes(1));
      expect(historyGet.mock.calls[0]?.[1]?.headers).toEqual(
        hadToken
          ? { "X-Copilot-Credential-Recovery-Token": "original-capability" }
          : {},
      );
      if (tokenState === "created during request")
        sessionStorage.setItem(key, "new-capability");
      if (tokenState === "cleared during request")
        sessionStorage.removeItem(key);
      const tokenBeforeResponse = sessionStorage.getItem(key);
      await act(async () => resolveHistory(historyResponse));

      if (hadToken) {
        expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
        expect(toast).not.toHaveBeenCalledWith(
          expect.objectContaining({
            title: "Could not recover the Copilot turn controls",
          }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
      } else {
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
        expect(toast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: "Could not recover the Copilot turn controls",
            description: expect.stringContaining("no cancellation token"),
          }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(useWorkflowYamlEditorStore.getState().lockKind).toBe("copilot");
      }
      expect(sessionStorage.getItem(key)).toBe(tokenBeforeResponse);
    },
  );

  it.each(["getItem", "setItem"] as const)(
    "A46 recovers an unanswered credential turn when sessionStorage %s throws after reload",
    async (method) => {
      const storage = vi
        .spyOn(Storage.prototype, method)
        .mockImplementation(() => {
          throw new DOMException("storage unavailable", "QuotaExceededError");
        });
      historyGet.mockResolvedValue({
        data: {
          ...historyResponse.data,
          pending_credential_requests: [],
          chat_history: [
            {
              sender: "user",
              content: "Sign in",
              turn_id: "turn-paused",
              created_at: new Date().toISOString(),
            },
          ],
        },
      });
      try {
        await renderChat();
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy(),
        );
        expect(toast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: "Could not recover the Copilot turn controls",
            description: expect.stringContaining("no cancellation token"),
          }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(useWorkflowYamlEditorStore.getState().lockKind).toBe("copilot");
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        expect(cancelPost).not.toHaveBeenCalled();
      } finally {
        storage.mockRestore();
      }
    },
  );

  it("restores the live title, description, and settings on rejection and refuses while committing YAML", async () => {
    changesState.hasChanges = true;
    const restore = vi.fn(restoreLive);
    await renderChat({ onRestore: restore });
    await submit("edit the workflow");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-07-09T00:00:00Z",
      });
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy(),
    );
    restore.mockClear();
    cancelPost.mockClear();
    act(() => useWorkflowYamlEditorStore.getState().setCommitInProgress(true));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    expect(restore).not.toHaveBeenCalled();
    expect(cancelPost).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "A YAML commit is in progress" }),
    );
    act(() => useWorkflowYamlEditorStore.getState().setCommitInProgress(false));
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(restore).toHaveBeenCalledOnce();
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: saveData.title,
      description: "Unsaved description",
      titleHasBeenGenerated: true,
    });
    expect(editorNodes[0]?.data).toMatchObject(saveData.settings);
    expect(editorNodes[0]?.data).toMatchObject({
      totpIdentifier: "unsaved-totp",
      adaptiveCaching: true,
      generateScriptOnTerminal: false,
      maxElapsedTimeMinutes: 25,
      cdpConnectHeaders: '{"Authorization":"********"}',
    });
    expect(editorNodes.find((node) => node.id === "loop")?.data).toMatchObject({
      loopValue: "unsaved_items",
      loopVariableReference: "{{ item }}",
    });
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      {
        parameterType: "context",
        key: "context",
        sourceParameterKey: "unsaved_source",
      },
    ]);
  });

  it("reserves Reject while server clearance is pending so a YAML commit cannot start", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveClear!: (value: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveClear = resolve;
        }),
    );
    apply.mockClear();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/clear-proposed-workflow",
      expect.anything(),
    );
    const owner = createYamlCommitOwner("wpid_1");
    expect(beginYamlCommit(owner)).toBe(false);
    expect(apply).not.toHaveBeenCalled();
    await act(async () => resolveClear({}));
    expect(apply).not.toHaveBeenCalled();
    expect(setEditorNodes).toHaveBeenCalledOnce();
    expect(editorNodes.find((node) => node.id === "loop")?.data).toMatchObject({
      loopValue: "unsaved_items",
      loopVariableReference: "{{ item }}",
    });
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(beginYamlCommit(owner)).toBe(true);
    finishYamlCommit(owner);
  });

  it("does not send keep_pending_proposal on a chat's first message (nothing pending yet)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]!.body.keep_pending_proposal).toBe(false);
  });

  it("interrupted_draft resyncs a stale typed Accept without applying it locally", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Newer draft",
    });
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-2",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/apply-proposed-workflow",
      expect.objectContaining({ owner_turn_id: "turn-1", revision: 1 }),
      { timeout: 30_000, signal: expect.any(AbortSignal) },
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("Proposal changed")).toBeTruthy();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(false);
  });

  it("keeps a failed typed Accept reserved when the row has its pre-Accept disposition", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });
    let finishReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishReload = () => resolve(historyResponse);
        }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    await act(async () => {
      finishReload();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
  });

  it("does not locally accept a typed proposal when its chat cannot be resolved", async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    expect(cancelPost).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("releases a legacy proposal after a proven no-write without applying it locally", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // A legacy proposal has no token, so the reload cannot prove it is the one that failed.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Replacement draft",
    });
    cancelPost.mockRejectedValueOnce({
      response: {
        status: 400,
        data: { detail: "Proposed copilot YAML is invalid" },
      },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/clear-proposed-workflow",
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Not saved")).toBeTruthy();
    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });

    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    expect(cancelPost).toHaveBeenCalledTimes(2);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("locks the gate while Accept is in flight so a second click cannot race it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    for (const name of ["Accept", "Always accept", "Reject"]) {
      expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
        true,
      );
    }
    expect(screen.getByText("Accepting…")).toBeTruthy();

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
  });

  it.each([false, true])(
    "locks History and New chat while an Accept is in flight (docked: %s)",
    async (docked) => {
      await renderChat({ docked });
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      let resolveApply: (value: unknown) => void = () => {};
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveApply = resolve;
          }),
      );
      const navigationLocked = () => {
        const controls = useCopilotHeaderStore.getState().controls;
        return docked
          ? [controls!.disabled, controls!.navigationLockedReason !== null]
          : ["History", "New chat"].map((name) =>
              // The reason joins the accessible name once a control is locked.
              screen
                .getByRole("button", { name: new RegExp(`^${name}`) })
                .matches(":disabled"),
            );
      };
      expect(navigationLocked()).toEqual([false, false]);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });

      // The server finishes an Accept regardless, so another chat must not take over the gate mid-apply.
      expect(navigationLocked()).toEqual([true, true]);
      // Nor may a new turn stage a proposal the late Accept would then clobber.
      await submit("change it again");
      expect(textarea().value).toBe("change it again");
      await act(async () => {
        resolveApply({ data: proposedWorkflowPayload() });
      });
      expect(navigationLocked()).toEqual([false, false]);
    },
  );

  // The live path that reaches this is the recovery poll, which no test in this file drives, so
  // the contract is asserted directly instead. See the PR's coverage-gaps note.
  describe("hydratedGateFailure", () => {
    // A claim the row can TIE TO A PROPOSAL. This fixture used to carry no metadata, which was
    // harmless only because the server derived the claim FROM the metadata - the two could not
    // disagree. It reports claim liveness independently now, so the combination below is a real
    // state with its own answer, and the assertions here would otherwise be testing it by
    // accident.
    const claimed = {
      proposed_claim_expires_in_seconds: 120,
      proposed_workflow_metadata: {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting" as const,
        workflow_run_id: null,
      },
      auto_accept: false,
    };
    const unclaimed = { ...claimed, proposed_claim_expires_in_seconds: null };
    const claimedByAnother = {
      ...claimed,
      proposed_workflow_metadata: null,
    };
    const held = {
      kind: "recover",
      alwaysAccept: false,
      token: null,
      wroteNothing: false,
      claimExpiresAtSeen: null,
    } as const;

    it("arms the fence when the row reports a claim", () => {
      const armed = hydratedGateFailure(claimed, null, 9_000);
      expect(armed?.kind).toBe("recover");
    });

    it("leaves an active hold alone when the row reports no claim", () => {
      // A read can overtake a POST that has not claimed yet, so "no claim" is not proof that
      // nothing is writing — releasing here is the hazard direction.
      expect(hydratedGateFailure(unclaimed, held, 9_000)).toBe(held);
    });

    it("stays clear when there is no hold and no claim", () => {
      expect(hydratedGateFailure(unclaimed, null, 9_000)).toBeNull();
    });

    it("keeps a confirmed save, which is the only copy of that workflow", () => {
      // `saved` means the apply returned 200 and its workflow is in hand while the canvas holds
      // the older draft. A recovery poll hydrating a row with no live claim must not drop it:
      // that discards the retry's only copy AND releases Save over a canvas known to be stale,
      // which is the overwrite this fence exists to prevent.
      const confirmed = {
        kind: "saved",
        alwaysAccept: false,
        token: null,
        wroteNothing: false,
        ownerTurnId: "turn-1",
        savedWorkflow: { workflow_id: "wf_saved" } as unknown as never,
      } as const;
      expect(hydratedGateFailure(unclaimed, confirmed, 9_000)).toBe(confirmed);
    });

    it("does not downgrade a confirmed save to a claim that may still be writing", () => {
      // The route clears the claim AFTER the workflow write, best-effort, so a row can still
      // report a claim for an Accept that already landed. Arming over `saved` would drop the
      // only copy of the confirmed workflow and give the hold a deadline that can release over
      // a stale canvas - trading what we know for what we are guessing.
      const confirmed = {
        kind: "saved",
        alwaysAccept: false,
        token: null,
        wroteNothing: false,
        ownerTurnId: "turn-1",
        savedWorkflow: { workflow_id: "wf_saved" } as unknown as never,
      } as const;
      expect(hydratedGateFailure(claimed, confirmed, 9_000)).toBe(confirmed);
    });

    it("still arms over every other hold when a row reports a claim", () => {
      // The suppression in `does not downgrade a confirmed save to a claim that may still be
      // writing` is specific to `saved`, which knows more than the row does.
      expect(hydratedGateFailure(claimed, held, 9_000)?.kind).toBe("recover");
      expect(hydratedGateFailure(claimed, null, 9_000)?.kind).toBe("recover");
    });

    it("lets a fresh row restate an outcome that claims nothing was saved", () => {
      const notSaved = {
        kind: "accept",
        alwaysAccept: false,
        token: null,
        wroteNothing: false,
      } as const;
      expect(hydratedGateFailure(unclaimed, notSaved, 9_000)).toBeNull();
    });

    it("does not narrate a claim the row cannot tie to a proposal", () => {
      // `recover` says "this may have saved" and sends the user to Try again. Both are false
      // about another writer's claim - and with no proposal there is no card to press it on, so
      // the message would name a button that is not rendered. Save still holds, via
      // unattributedClaimDeadline, which says only what is true.
      expect(hydratedGateFailure(claimedByAnother, null, 9_000)).toBeNull();
    });
  });

  describe("fenceBaselineFor", () => {
    it("takes its reading only when no fence is open yet", () => {
      expect(fenceBaselineFor(undefined, 120_000)).toBe(120_000);
    });

    it("never moves an open fence's baseline", () => {
      expect(fenceBaselineFor(400, 300_000)).toBe(400);
    });

    it("keeps a blind fence blind", () => {
      // null is the ANSWER, not a missing one: the terminal pass treats an absent baseline as
      // grounds to hold, so letting an ordinary poll supply one from another writer's claim
      // would hand that rule a number to compare against and release.
      expect(fenceBaselineFor(null, 300_000)).toBeNull();
    });
  });

  describe("extendedClaimHold", () => {
    it("arms from a row when nothing is held yet", () => {
      expect(extendedClaimHold(null, 121_000)).toBe(121_000);
    });

    it("keeps the hold when a row reports no claim", () => {
      // A row covers ONE chat. The server reads the claim off that chat's own proposal blob, so
      // "no claim here" is not "no claim on this workflow" - and retiring on it releases Save
      // under another writer's live Accept.
      expect(extendedClaimHold(121_000, null)).toBe(121_000);
    });

    it("stays retired when nothing has ever armed it", () => {
      expect(extendedClaimHold(null, null)).toBeNull();
    });

    it("extends to a later claim", () => {
      expect(extendedClaimHold(121_000, 300_000)).toBe(300_000);
    });

    it("keeps the LATER deadline when a row reports a shorter one", () => {
      // MAX, NOT LAST. A chat-scoped read cannot say whose claim it is seeing, so a second
      // chat's shorter live claim is not evidence the first writer finished - taking the last
      // reading would release Save at 250 while a writer holding until 300 is still writing.
      expect(extendedClaimHold(300_000, 250_000)).toBe(300_000);
    });
  });

  describe("unattributedClaimDeadline", () => {
    const row = {
      proposed_claim_expires_in_seconds: 120,
      proposed_workflow_metadata: null,
    };

    it("holds until the reported lease runs out", () => {
      expect(unattributedClaimDeadline(row, 1_000)).toBe(121_000);
    });

    it("stays out of the way when the claim belongs to a visible proposal", () => {
      // That claim is the gate's business, not the workflow's - arming both would hold Save
      // twice for one write and leave this one outliving the gate that explains it.
      expect(
        unattributedClaimDeadline(
          {
            ...row,
            proposed_workflow_metadata: {
              owner_turn_id: "turn-1",
              revision: 1,
              canonical_fingerprint: "canonical-1",
              disposition: "accepting",
              workflow_run_id: null,
            },
          },
          1_000,
        ),
      ).toBeNull();
    });

    it("does not hold when no claim is reported", () => {
      expect(
        unattributedClaimDeadline(
          { ...row, proposed_claim_expires_in_seconds: null },
          1_000,
        ),
      ).toBeNull();
    });
  });

  it("retains the canvas and reservation when canonical recovery is unreadable", async () => {
    const onWorkflowUpdate = vi.fn();
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({ workflow_id: "wf_saved" });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: savedWorkflow }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      savedWorkflow,
      expect.anything(),
    );
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("refuses a new send that would clear an unresolved Accept gate", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("keeps Save held when a terminal recheck still finds the original proposal", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "baseline",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata:
            historyResponse.data.proposed_workflow_metadata,
        }),
      );
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("locks proposal actions after a failed reload, and Try again reconciles instead of re-sending Accept", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });
    historyGet.mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    for (const name of ["Accept", "Always accept", "Reject"]) {
      expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
        true,
      );
    }
    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
  });

  it("shows a failed reload after a follow-up turn inline, with only the reload actionable", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));

    // The turn ends without a new draft, so the kept proposal is re-read from the chat row.
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);

    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(false);
  });

  it("holds a browser-queued prompt until a pending Accept settles, then sends it", async () => {
    const view = await renderChat({
      requiresLiveBrowser: true,
      isLiveBrowserReady: true,
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    view.rerender(
      <WorkflowCopilotChat
        docked={false}
        requiresLiveBrowser
        isLiveBrowserReady={false}
      />,
    );
    await submit("queued follow-up");
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    view.rerender(
      <WorkflowCopilotChat
        docked={false}
        requiresLiveBrowser
        isLiveBrowserReady
        liveBrowserSessionId="pbs_harness"
      />,
    );
    const sent = (message: string) =>
      streamCalls.some((call) => call.body.message === message);
    // Let any send the effect would start reach the stream before asserting it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(sent("queued follow-up")).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(sent("queued follow-up")).toBe(true));
  });

  it("holds an initial message that arrives during a pending Accept, then sends it", async () => {
    const view = await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // A slow Accept outlives the auto-send fallback window, which must not consume the message.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    view.rerender(
      <WorkflowCopilotChat docked={false} initialMessage="test the run" />,
    );
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });
    vi.useRealTimers();
    const sent = (message: string) =>
      streamCalls.some((call) => call.body.message === message);
    // Let any send the effect would start reach the stream before asserting it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(sent("test the run")).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(sent("test the run")).toBe(true));
  });

  it.each([
    ["live", 240],
    ["expired", null],
    // An API instance that predates the field omits it; its row still names the claim.
    ["unreported by an older API", undefined],
  ] as const)(
    "keeps an accepting proposal reserved with a %s server claim",
    async (_label, claimTtlSeconds) => {
      const metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "review_untested" as const,
        workflow_run_id: null,
      };
      await renderChat();
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(
          proposalResponse("Draft ready.", {
            proposed_workflow_metadata: metadata,
          }),
        );
        streamCalls[0]!.resolve();
      });
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        ...metadata,
        disposition: "accepting" as unknown as "review_untested",
      } as typeof metadata;
      leaseDecrementingFrom(claimTtlSeconds);
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });

      expect(await screen.findByRole("alert")).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Try again" }).matches(":disabled"),
      ).toBe(false);
      for (const name of ["Accept", "Reject"]) {
        expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
          true,
        );
      }
    },
  );

  it("does not revive the previous chat's gate when a reload retry lands after New chat", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();

    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    let finishReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      finishReload();
    });

    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps a newer chat's reload failure when the previous chat's reload retry lands", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    let finishOldReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOldReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });

    // The new chat stages its own proposal, and its Reject hits a conflict whose reload fails.
    await submit("build a different workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    await act(async () => {
      streamCalls[2]!.onMessage(
        proposalResponse("Second draft ready.", {
          workflow_copilot_chat_id: "chat-2",
          turn_id: "turn-2",
          narrative_payload: proposalNarrativePayload({ turnId: "turn-2" }),
        }),
      );
      streamCalls[2]!.resolve();
    });
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();

    await act(async () => {
      finishOldReload();
    });

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);
  });

  it.each(["the chat unmounts", "the claim lease expires"] as const)(
    "retains the Accept reservation across %s",
    async (release) => {
      const view = await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
      const locked = (name: string) =>
        screen
          .getByRole("button", { name: new RegExp(`^${name}`) })
          .matches(":disabled");
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      historyGet.mockImplementation((path: string) =>
        path !== "/workflows/wpid_1"
          ? Promise.resolve(historyResponse)
          : Promise.reject(new Error("network down")),
      );
      if (release === "the claim lease expires") {
        historyResponse.data.proposed_workflow = proposedWorkflowPayload();
        historyResponse.data.proposed_workflow_metadata = {
          owner_turn_id: "turn-1",
          revision: 1,
          canonical_fingerprint: "canonical-1",
          disposition: "accepting" as unknown as "review_untested",
          workflow_run_id: null,
        } as typeof historyResponse.data.proposed_workflow_metadata;
        leaseDecrementingFrom(1.5);
      }
      let loseApplyResponse: () => void = () => {};
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            loseApplyResponse = () => reject(new Error("Network Error"));
          }),
      );
      expect(saveHeld()).toBe(false);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(saveHeld()).toBe(true);

      await act(async () => {
        loseApplyResponse();
      });
      expect(await screen.findByRole("alert")).toBeTruthy();
      expect(saveHeld()).toBe(true);
      expect(locked("History")).toBe(true);
      expect(locked("New chat")).toBe(true);
      expect(
        screen.getByRole("button", { name: "New chat" }).title,
      ).toBeTruthy();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "New chat" }));
      });
      expect(saveHeld()).toBe(true);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      });
      expect(saveHeld()).toBe(true);

      if (release === "the chat unmounts") {
        await act(async () => view.unmount());
        expect(
          useWorkflowYamlEditorStore.getState().pendingAccepts.wpid_1,
        ).toBeDefined();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
      } else {
        await act(
          async () => new Promise((resolve) => setTimeout(resolve, 800)),
        );
        expect(saveHeld()).toBe(true);
        expect(locked("History")).toBe(true);
        expect(locked("New chat")).toBe(true);
      }
    },
    10_000,
  );

  it.each([
    ["a live server claim", "live-claim"],
    ["a chat row that will not load", "row-unreadable"],
    ["an API that cannot report the claim", "older-api"],
  ] as const)(
    "keeps saves and sends held while %s leaves the Accept unaccounted for",
    async (_label, mode) => {
      await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      if (mode === "row-unreadable") {
        historyGet.mockImplementation(() =>
          Promise.reject(new Error("network down")),
        );
      } else {
        historyResponse.data.proposed_workflow = proposedWorkflowPayload();
        historyResponse.data.proposed_claim_expires_in_seconds =
          mode === "live-claim" ? 120 : undefined;
        if (mode === "older-api") {
          // An instance that predates the field omits it; the row still names the claim,
          // and that claim can run for the backend's full five-minute lease.
          historyResponse.data.proposed_workflow_metadata = {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "accepting" as unknown as "review_untested",
            workflow_run_id: null,
          } as typeof historyResponse.data.proposed_workflow_metadata;
        }
      }
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(await screen.findByRole("alert")).toBeTruthy();

      // A new turn would stage a proposal over the one the server may be writing, and
      // sending clears the gate failure, which would release the hold with it.
      await submit("change it again");
      expect(postStreaming).toHaveBeenCalledTimes(1);
      expect(saveHeld()).toBe(true);

      // A fence sized to a re-read rather than to the lease would have lapsed by now and let
      // the next Save duplicate the version the backend is still writing.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 3_500));
      });
      expect(saveHeld()).toBe(true);
    },
    10_000,
  );

  it("keeps the fence when the editor cannot apply the workflow the server already saved", async () => {
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("could not parse the saved workflow");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The server saved and cleared the proposal, but the response was lost, so recovery
    // reads canonical — and the editor cannot take it.
    historyResponse.data.proposed_workflow = null;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: proposedWorkflowPayload() }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The canvas still holds the pre-Accept graph, so releasing here would write it back
    // over the version the server kept.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null,
    ).toBe(true);
  });

  it("retries the workflow the Accept confirmed, instead of re-reading the chat row", async () => {
    let editorAccepts = false;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The apply SUCCEEDS - 200, the saved workflow is in hand - but the editor throws.
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    const readsBefore = historyGet.mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // Not an ambiguous outcome, so recovery must not run: no chat-row read at all.
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(historyGet.mock.calls.length).toBe(readsBefore);
    expect(saveHeld()).toBe(true);
    // Hydration refuses to arm a claim over this gate, which is only safe because no SECOND
    // Accept can be started from it to claim one - the action row is locked here.
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);

    // Try again re-applies the workflow the server confirmed.
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    expect(applied).toContainEqual(savedWorkflow);
    await waitFor(() => expect(saveHeld()).toBe(false));
    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
  });

  it("keeps auto-accept on when a row read from before a saved Always accept lands last", async () => {
    // The apply's 200 means the server may have written auto_accept, so a row read issued before
    // it can no longer be trusted, even though the editor refused the workflow. The held row
    // reports an unattributed claim, which arms the Save hold from any row: that is how the test
    // knows the read landed.
    let editorAccepts = false;
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // A follow-up turn with no new draft re-reads the row for the bypassed proposal. Hold it open.
    let staleReadIssued = false;
    let releaseStaleRead: () => void = () => {};
    const staleRow = {
      data: {
        ...historyResponse.data,
        proposed_workflow: null,
        proposed_workflow_metadata: null,
        proposed_claim_expires_in_seconds: 100,
        auto_accept: false,
      },
    };
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          staleReadIssued = true;
          releaseStaleRead = () => resolve(staleRow);
        }),
    );
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    await waitFor(() => expect(staleReadIssued).toBe(true));

    cancelPost.mockResolvedValueOnce({
      data: proposedWorkflowPayload({ workflow_id: "wf_saved" }),
    });
    historyResponse.data.auto_accept = true;
    historyResponse.data.proposed_workflow = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(await screen.findByText("Auto-accepting")).toBeTruthy();
    expect(saveHeld()).toBe(false);

    await act(async () => {
      releaseStaleRead();
    });
    await waitFor(() => expect(saveHeld()).toBe(true));
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByText("Auto-accepting")).toBeTruthy();
  });

  it("does not hold Save when a post-Accept read fails, since no card is shown", async () => {
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      applied.push(workflow);
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A SUCCESSFUL Always accept: 200, the editor takes the workflow. auto_accept was off, so
    // `alwaysAccept !== autoAcceptRef.current` and the post-Accept re-read is issued. It fails.
    cancelPost.mockResolvedValueOnce({
      data: proposedWorkflowPayload({ workflow_id: "wf_ok" }),
    });
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    await waitFor(() => expect(applied.length).toBeGreaterThan(0));

    // The Accept SUCCEEDED and the editor loaded the result as its clean baseline.
    expect(screen.queryByText("Saved, not shown")).toBeNull();
    // No card renders here (the proposal is cleared and the gate is not `saved`), so there is no
    // Try again and no named exit. A hold would be unreleasable until a reload - and a failed row
    // read is an availability fact, not evidence that canonical moved under the editor.
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(saveHeld()).toBe(false);
  });

  it("holds Save whenever the reload card is on screen, whichever read armed it", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A follow-up turn with no new draft re-reads the row for the bypassed proposal (:5132).
    // That read FAILS. The proposal is still staged, so the reload card has a subject.
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });

    // This caller has no evidence canonical moved, but the CARD is what carries the contract:
    // it tells the user the canvas may be stale, so Save must not sit live beside it.
    expect(screen.queryByText("Couldn't reload")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // And the hold ENDS WITH THE CARD, which is the property this shape was chosen for. Try again
    // re-reads successfully, the gate clears, the card goes - and Save must come back. A hold
    // derived from the failed READ rather than from the card outlives the card here, which is how
    // the earlier shape stranded the user with a message that was no longer on screen.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await waitFor(() =>
      expect(screen.queryByText("Couldn't reload")).toBeNull(),
    );
    expect(saveHeld()).toBe(false);
  });

  it("takes auto-accept from the server after a saved retry, not from the attempt", async () => {
    let editorAccepts = false;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    // Mount with auto-accept OFF, the way the session actually starts: the server only
    // persists it during the apply below. Seeding it true before mount would let hydration
    // set the flag and the test would pass without reading anything.
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    // The apply persists auto_accept server-side. Its 200 does not carry that fact - the
    // route writes it after the workflow write, inside a best-effort swallow - so the only
    // way the client can know is to read it back.
    historyResponse.data.auto_accept = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(applied).toContainEqual(savedWorkflow);

    // Reading one field must not re-hydrate the row: the gate this retry just cleared may
    // not come back.
    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("Confirming\u2026")).toBeNull();
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(screen.queryByText("Saved, not shown")).toBeNull();

    // And the client now holds the server's value, which is what the user experiences.
    //
    // CHANGED BY THE #17099 MERGE, AND THE MECHANISM IS GONE WHILE THE CONCERN SURVIVES. This
    // used to assert that the next auto-applicable proposal AUTO-APPLIED, because the client's
    // flag drove that decision. #17099 moved the decision to the server -
    // shouldAutoApplyWorkflowResponse no longer takes autoAccept and now requires
    // `workflow_applied === true` - so that assertion can no longer distinguish a client that
    // re-read auto_accept from one that did not. Adding `workflow_applied: true` to the response
    // would turn this green in a line and make it VACUOUS FOR ITS OWN PURPOSE: it would pass
    // whether or not the read ever happened.
    //
    // The concern - the client must not carry a STALE auto_accept forward - is still live, so it
    // is asserted where the value still has an observable effect. The chip renders on exactly
    // `autoAccept && workflowCopilotChatId`, so its presence IS the client's flag, and it is the
    // control through which the user turns auto-accept off. Asserting the request body would be
    // stronger, but nothing sends this state: the apply POST sends the button (`alwaysAccept`)
    // and clearProposedWorkflow's only caller passes a literal false.
    expect(await screen.findByText("Auto-accepting")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Turn off/ })).toBeTruthy();
  });

  it("does not carry a saved retry's auto-accept read into a new chat", async () => {
    let editorAccepts = false;
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    historyResponse.data.auto_accept = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    // Hold the post-retry read open so the chat switch happens while it is in flight.
    let releaseRow: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseRow = () => resolve(historyResponse);
        }),
    );
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    // Clearing the gate unlocks navigation, so the user can start a new chat before the old
    // chat's row comes back - and New chat resets auto-accept to off.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      releaseRow();
    });

    // The old chat's setting must not land in the new one: its next auto-applicable proposal
    // waits for review instead of being applied locally past the gate.
    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("start something else");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("New draft.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[1]!.resolve();
    });
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  it("reserves the workflow until Reject settles and shows the discarded receipt", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // Reject reserves the graph until server clearance and rollback finish.
    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    await submit("actually, do it differently");
    expect(postStreaming).toHaveBeenCalledTimes(1);
    await act(async () => {
      releaseReject();
    });

    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(
      screen.getByText("Discarded — canvas reverted to the previous version"),
    ).toBeTruthy();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("does not let a late Reject clear the gate of a chat the user navigated to", async () => {
    await renderChat({ docked: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    // The user picks another chat from History. That chat already has a proposal pending, so
    // it hydrates its own review gate - nothing here bumps the send epoch.
    historyResponse.data.workflow_copilot_chat_id = "chat-2";
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    await act(async () => {
      releaseReject();
    });

    // The reject belonged to the chat the user left. Landing it here clears the selected
    // chat's proposal while its candidate is still pending on the server, leaving no gate.
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("still completes a Reject when the history navigation it raced fails", async () => {
    await renderChat({ docked: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    // The user tries another chat and the load FAILS, so they stay where they are.
    historyGet.mockImplementationOnce(() =>
      Promise.reject(new Error("network down")),
    );
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    await act(async () => {
      releaseReject();
    });

    // Nothing moved: the user is in the chat they rejected from, the server cleared the
    // candidate, so the draft and its gate must go. Bailing here would leave the rejected
    // draft on the canvas and saveable.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
  });

  it("discards a history load the user abandoned", async () => {
    const onWorkflowUpdate = vi.fn();
    await renderChat({ docked: true, onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A history load the user will walk away from, carrying that chat's own settings.
    let releaseHistory: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseHistory = () =>
            resolve({
              data: {
                ...historyResponse.data,
                workflow_copilot_chat_id: "chat-2",
                auto_accept: true,
                proposed_workflow: proposedWorkflowPayload(),
              },
            });
        }),
    );
    await act(async () => {
      void useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    // The user gives up on it and starts a new chat, which clears the board.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    // Only now does the abandoned load answer.
    await act(async () => {
      releaseHistory();
    });

    // It must not write the chat the user left back over the one they are in: no gate from a
    // proposal belonging to chat-2.
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();

    // And it must not restore that chat's Always accept either, which would auto-apply the
    // next proposal here without review.
    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("something else entirely");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Fresh draft.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[1]!.resolve();
    });
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  it("blocks a later turn while an earlier Accept remains unresolved", async () => {
    await renderChat({ onWorkflowUpdate: vi.fn() });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("keeps the recovery gate's exit on screen after hydration clears the proposal, with its actions still locked", async () => {
    const view = await renderChat({ docked: true });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // The Accept's outcome is unknown and the row reports a live claim, so the gate parks on
    // `recover` - whose Save reason tells the user to use Try again on the review gate.
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Hydration clears the proposal while the resumed recovery awaits canonical evidence.
    view.unmount();
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    let finishCanonical!: () => void;
    const canonical = new Promise<{ data: WorkflowApiResponse }>((resolve) => {
      finishCanonical = () => resolve({ data: saveData.workflow });
    });
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? canonical
        : Promise.resolve(historyResponse),
    );
    await renderChat({ docked: true });

    // Half one: the exit the Save reason names is on screen.
    const retry = await screen.findByRole("button", { name: "Try again" });
    expect(retry.matches(":disabled")).toBe(false);
    // Half two, and it is the half that stops this fix becoming a defect: rendering the card must
    // NOT bring the proposal actions back. `gateActionable` is true here; what holds the line is
    // that ReviewGateCard renders no action row without a proposal to act on.
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(saveHeld()).toBe(true);

    await act(async () => finishCanonical());
    await waitFor(() => expect(saveHeld()).toBe(false));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("settles the gate and leaves the chat usable when a save moved canonical before Accept", async () => {
    const onWorkflowUpdate = vi.fn();
    await stageProposalOn("download_invoice_pdf", { onWorkflowUpdate });

    // The user saved that block themselves, so canonical moved: apply refuses with a 409 raised
    // before it creates any version, and the row stops showing the proposal. Nothing here is
    // unknown, so the fence for an unknown write may not arm over it.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await acceptRefusedWith(409);

    expect(
      await screen.findByText(
        "This workflow changed, so your Accept didn't go through and the proposal is gone. Send Copilot a new message to propose it again.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Confirming…")).toBeNull();
    // Nothing to retry and nothing to act on: Try again would re-send an Accept the server has
    // already refused, and Reject would put the pre-proposal canvas back over the user's save.
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    // The edit that moved canonical was the user's own save, so there is nothing unsaved to
    // protect: the canvas re-reads canonical, and that fresh read is what releases Save.
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().saveBlockedReason,
      ).toBeNull(),
    );
    expect(onWorkflowUpdate).toHaveBeenCalled();

    // The half a cleared spinner does not cover: the chat surface itself was unusable until a
    // page reload. The composer's textarea stays editable under the fence, so only the send
    // itself shows this.
    expect(
      screen.getByRole("button", { name: "Send" }).matches(":disabled"),
    ).toBe(false);
    await submit("put the sheet name back");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toBeNull();
  });

  it("holds Save past the terminal card when the canvas has edits the user never saved", async () => {
    const onWorkflowUpdate = vi.fn();
    await stageProposalOn("download_invoice_pdf", { onWorkflowUpdate });

    // Same server state as above - canonical moved, the proposal is gone - but this canvas
    // carries edits only the user has. A re-read would destroy them, so Save holds instead.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    await acceptRefusedWith(409);

    expect(screen.queryByText("Confirming\u2026")).toBeNull();
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
      "this canvas may be out of date",
    );
    expect(
      useWorkflowHasChangesStore.getState().saveBlockedReason,
    ).not.toContain("couldn't confirm");

    await submit("put the sheet name back");
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
      "this canvas may be out of date",
    );
    expect(onWorkflowUpdate).not.toHaveBeenCalled();
  });

  it("keeps Save held when the refresh after a moved canonical cannot read the workflow", async () => {
    const onWorkflowUpdate = vi.fn();
    await stageProposalOn("download_invoice_pdf", { onWorkflowUpdate });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? Promise.reject(new Error("Network Error"))
        : Promise.resolve(historyResponse),
    );
    await acceptRefusedWith(409);

    await waitFor(() =>
      expect(
        historyGet.mock.calls.some(([path]) => path === "/workflows/wpid_1"),
      ).toBe(true),
    );
    // Nothing replaced the canvas, so it is still older than canonical and Save may not write it.
    await act(async () => {});
    expect(onWorkflowUpdate).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
      "this canvas may be out of date",
    );
  });

  it("keeps edits made while the refresh after a moved canonical was in flight", async () => {
    const onWorkflowUpdate = vi.fn();
    await stageProposalOn("download_invoice_pdf", { onWorkflowUpdate });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    let releaseCanonical: (value: unknown) => void = () => {};
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? new Promise((resolve) => {
            releaseCanonical = resolve;
          })
        : Promise.resolve(historyResponse),
    );
    await acceptRefusedWith(409);
    await waitFor(() =>
      expect(
        historyGet.mock.calls.some(([path]) => path === "/workflows/wpid_1"),
      ).toBe(true),
    );

    useWorkflowHasChangesStore.setState({ hasChanges: true });
    await act(async () => {
      releaseCanonical({
        data: proposedWorkflowPayload({ workflow_id: "wf_canonical" }),
      });
    });

    expect(onWorkflowUpdate).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
      "this canvas may be out of date",
    );
  });

  it.each([404, 500])(
    "keeps confirming when Accept is refused with %i, which does not prove the write never started",
    async (status) => {
      await stageProposalOn("download_invoice_pdf");

      // Same row as the case above - proposal gone, no claim - so only the status separates them.
      // 404 is the apply route saying it could not resolve the chat id, which makes a later read
      // of that same id worthless as evidence; 500 is not on its refusal ladder at all.
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.reject(new Error("Canonical unreadable"))
          : Promise.resolve(historyResponse),
      );
      await acceptRefusedWith(status);

      expect(await screen.findByText("Confirming…")).toBeTruthy();
      expect(screen.queryByText(/this proposal is gone/)).toBeNull();
      expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
        "Try again on the review gate",
      );
    },
  );

  it("holds the fence at its deadline while a claim is still outstanding", async () => {
    await stageProposalOn("download_invoice_pdf");

    vi.useFakeTimers();
    try {
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting",
        workflow_run_id: null,
      };
      historyResponse.data.proposed_claim_expires_in_seconds = 20;
      await acceptRefusedWith(409);
      expect(screen.getByText("Confirming…")).toBeTruthy();

      // An instance that cannot report a remainder still names the claim through the proposal's
      // disposition, so the deadline pass arrives with a write possibly in flight. A refusal
      // proving OUR Accept wrote nothing says nothing about that writer's.
      delete (
        historyResponse.data as {
          proposed_claim_expires_in_seconds?: number | null;
        }
      ).proposed_claim_expires_in_seconds;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500_000);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(screen.getByText("Couldn't reload")).toBeTruthy();
      expect(screen.queryByText("Proposal changed")).toBeNull();
      expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toContain(
        "Try again on the review gate",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the confirmed-save retry reachable after hydration clears the proposal", async () => {
    let editorAccepts = true;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    await renderChat({ docked: true, onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // The apply returns 200 and the editor throws, so the gate parks on `saved`.
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    editorAccepts = false;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // A hydration lands carrying no proposal - which is the truth after a save: the server
    // cleared it. The recovery poll reaches this path without touching the fence.
    historyGet.mockImplementationOnce(() =>
      Promise.resolve({
        data: {
          ...historyResponse.data,
          workflow_copilot_chat_id: "chat-2",
          proposed_workflow: null,
        },
      }),
    );
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    // The hold has no deadline, so its exit has to survive the proposal going away. A card
    // with no Try again would be a hold the user cannot end except by reloading the page.
    const retry = await screen.findByRole("button", { name: "Try again" });
    expect(retry.matches(":disabled")).toBe(false);

    editorAccepts = true;
    await act(async () => {
      fireEvent.click(retry);
    });
    expect(applied).toContainEqual(savedWorkflow);
    await waitFor(() => expect(saveHeld()).toBe(false));
  });

  it("holds New chat during startup, then permits a fresh chat without Always accept", async () => {
    const onWorkflowUpdate = vi.fn();
    // The INITIAL history read pends, carrying the old chat's Always accept.
    let releaseMount: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseMount = () =>
            resolve({
              data: {
                ...historyResponse.data,
                workflow_copilot_chat_id: "chat-old",
                auto_accept: true,
              },
            });
        }),
    );
    await renderChat({ docked: true, onWorkflowUpdate });
    expect(
      screen.getByRole("button", { name: "New chat" }).matches(":disabled"),
    ).toBe(true);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      releaseMount();
    });

    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "New chat" })),
    );
    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("something new");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft.", {
          turn_id: "turn-9",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  // The gate owner's row decides WHICH site renders the card, so both shapes are driven here
  // against each other. A narrative row renders it inline at the narrative site; a plain row
  // has no narrative branch and must fall to the message footer. Both suppress the standalone
  // fallback, so each arm also asserts EXACTLY ONE card - a second card is as wrong as none,
  // and a single-element query would pass either way.
  it.each([
    ["a narrative row", true],
    ["a plain row with no narrative", false],
  ])(
    "keeps the confirmed-save exit reachable when the owner card is inline: %s",
    async (_label, withNarrative) => {
      let editorAccepts = true;
      const applied: WorkflowApiResponse[] = [];
      const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
        if (!editorAccepts) {
          throw new Error("editor could not load it");
        }
        applied.push(workflow);
      });
      await renderChat({ docked: true, onWorkflowUpdate });
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });

      const savedWorkflow = proposedWorkflowPayload({
        workflow_id: "wf_saved",
      }) as unknown as WorkflowApiResponse;
      editorAccepts = false;
      cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(await screen.findByText("Saved, not shown")).toBeTruthy();
      expect(saveHeld()).toBe(true);

      // A hydration for THIS chat lands: the proposal is correctly gone - the server cleared it
      // on the save - but the turn that owned it is still in the history, so its card still
      // renders inline, which suppresses the standalone gate.
      historyGet.mockImplementationOnce(() =>
        Promise.resolve({
          data: {
            ...historyResponse.data,
            proposed_workflow: null,
            chat_history: [
              {
                sender: "ai",
                content: "Draft ready.",
                created_at: new Date().toISOString(),
                ...(withNarrative
                  ? { narrative_payload: proposalNarrativePayload() }
                  : {}),
              },
            ],
          },
        }),
      );
      await act(async () => {
        await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
          workflow_copilot_chat_id: "chat-rehydrate",
        } as Parameters<
          NonNullable<
            NonNullable<
              ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
            >["onSelectChat"]
          >
        >[0]);
      });

      // The hold has no deadline, so the exit has to survive the proposal going away - including
      // when the inline owner card is the one rendering the gate.
      expect(await screen.findByText("Saved, not shown")).toBeTruthy();
      expect(screen.getAllByText("Saved, not shown")).toHaveLength(1);
      const retry = await screen.findByRole("button", { name: "Try again" });
      expect(retry.matches(":disabled")).toBe(false);

      editorAccepts = true;
      await act(async () => {
        fireEvent.click(retry);
      });
      expect(applied).toContainEqual(savedWorkflow);
      await waitFor(() => expect(saveHeld()).toBe(false));
      // The RECEIPT, which is the whole reason the `saved` gate carries its own owner turn:
      // hydration cleared pendingProposalTurnId, so without the carried id the retry marks
      // nothing and a save that definitely landed reports nothing. Asserting the applied
      // workflow alone leaves that regression invisible - both arms stay green with the carried
      // id replaced by null.
      //
      // Only the narrative row can show it: `settled` is computed at the narrative render site
      // and hardcoded to null at the message-footer and fallback sites, so a plain row's card
      // simply disappears when the gate clears. That asymmetry is a separate gap, recorded here
      // rather than asserted away - the plain arm checks the half it CAN observe.
      if (withNarrative) {
        expect(
          await screen.findByText("Accepted — saved to the workflow"),
        ).toBeTruthy();
      } else {
        await waitFor(() =>
          expect(screen.queryByText("Saved, not shown")).toBeNull(),
        );
      }
    },
  );

  it("keeps Retry available until a cleared proposal has canonical confirmation", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("keeps the fence when the apply succeeds but the editor cannot take the saved workflow", async () => {
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("could not parse the saved workflow");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The apply returns 200 — the version exists — and the canonical re-read that would
    // catch the canvas up is unavailable.
    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    historyGet.mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The studio's own handler shows "Update failed" and re-throws so this boolean is honest;
    // the wrapper must not add a second toast for the same event.
    expect(toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Update failed" }),
    );
    // The server holds the accepted version and the canvas still holds the pre-Accept graph,
    // so releasing here would write the stale one back over it.
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null,
      ).toBe(true),
    );
  });

  it("keeps manual retries inside the outstanding Accept reservation", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_claim_expires_in_seconds = 0.5;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    historyGet.mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it.each([
    ["reports the remainder", 120],
    ["is too old to report it", undefined],
  ] as const)(
    "restores the Accept fence when a reload lands while a server that %s still holds its claim",
    async (_label, claimTtlSeconds) => {
      // The tab reloads after Accept reached the backend and before it finished.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting" as unknown as "review_untested",
        workflow_run_id: null,
      } as typeof historyResponse.data.proposed_workflow_metadata;
      leaseDecrementingFrom(claimTtlSeconds);

      await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );

      // Hydrating this as an ordinary pending proposal would leave Save and the gate live
      // while the server may still be creating the version.
      await waitFor(() =>
        expect(
          useWorkflowHasChangesStore.getState().saveBlockedReason !== null,
        ).toBe(true),
      );
      for (const name of ["Accept", "Reject"]) {
        expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
          true,
        );
      }
    },
  );

  it("does not call a failed Accept applied when the surviving proposal still matches canonical", async () => {
    const onWorkflowUpdate = vi.fn();
    useWorkflowHasChangesStore.setState({
      getSaveData: () =>
        ({
          ...saveData,
          workflow: { ...saveData.workflow, version: 3 },
        }) as unknown as WorkflowSaveData,
    });
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The row still carries the proposal AND its metadata, which the history route only
    // returns while canonical matches the fingerprint the proposal was built against — so
    // canonical being ahead of the editor says the editor is stale, not that this Accept won.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested" as const,
      workflow_run_id: null,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: { ...proposedWorkflowPayload(), version: 4 } }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // Whichever failure it settles on, it may not be the one that claims success.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it.each([false, true])(
    "settles a surviving proposal from a newer canonical version only with no-write proof: %s",
    async (wroteNothing) => {
      const onWorkflowUpdate = vi.fn();
      useWorkflowHasChangesStore.setState({
        getSaveData: () =>
          ({
            ...saveData,
            workflow: { ...saveData.workflow, version: 3 },
          }) as unknown as WorkflowSaveData,
      });
      await renderChat({ onWorkflowUpdate });
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      const created = { ...proposedWorkflowPayload(), version: 4 };
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: created } : historyResponse,
        ),
      );
      cancelPost.mockRejectedValueOnce(
        wroteNothing
          ? { response: { status: 409 } }
          : new Error("Network Error"),
      );

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(await screen.findByRole("alert")).toBeTruthy();
      expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
        created,
        expect.anything(),
      );
      expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
      expect(
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null,
      ).toBe(!wroteNothing);
      expect(
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .getAttribute("aria-label")
          ?.includes("paused"),
      ).toBe(!wroteNothing);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance !== null,
      ).toBe(!wroteNothing);
      if (wroteNothing) expect(screen.getByText("Not saved")).toBeTruthy();
    },
  );

  it("clears a block Generate refused during a pending save without sending it", async () => {
    useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    await renderChat();
    try {
      await act(async () => {
        expect(beginSaveTransaction(owner)).toBe(true);
        useCopilotActionStore.getState().requestBuild({
          blockLabel: "extract_titles",
          prompt: "read titles",
        });
      });
      expect(useCopilotActionStore.getState().pendingBuild).toBeNull();
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull();
      expect(postStreaming).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();

      await act(async () => finishSaveTransaction(owner));
      expect(postStreaming).not.toHaveBeenCalled();
      await submit("Continue editing the workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledOnce());
      expect(streamCalls[0]?.body).toMatchObject({
        message: "Continue editing the workflow",
        target_block_label: null,
      });
    } finally {
      finishSaveTransaction(owner);
      useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    }
  });

  it("clears a block Generate refused during recording and disarms its target", async () => {
    useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    useRecordingStore.setState({ isRecording: true });
    await renderChat();
    try {
      await act(async () => {
        useCopilotActionStore.getState().requestBuild({
          blockLabel: "extract_titles",
          prompt: "read titles",
        });
      });
      expect(useCopilotActionStore.getState().pendingBuild).toBeNull();
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull();
      expect(postStreaming).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();

      await act(async () => useRecordingStore.setState({ isRecording: false }));
      await submit("Continue editing the workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledOnce());
      expect(streamCalls[0]?.body).toMatchObject({
        message: "Continue editing the workflow",
        target_block_label: null,
      });
    } finally {
      useRecordingStore.setState({ isRecording: false });
      useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    }
  });

  it("clears a block Generate dropped on unmount before deferred reservation", async () => {
    useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    const view = await renderChat();
    try {
      act(() => {
        useCopilotActionStore.getState().requestBuild({
          blockLabel: "extract_titles",
          prompt: "read titles",
        });
      });
      expect(useCopilotActionStore.getState().pendingBuild).toBeNull();
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBe(
        "extract_titles",
      );
      expect(postStreaming).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();

      await act(async () => view.unmount());
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull();
      expect(postStreaming).not.toHaveBeenCalled();

      await renderChat();
      await submit("Continue editing the workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledOnce());
      expect(streamCalls[0]?.body).toMatchObject({
        message: "Continue editing the workflow",
        target_block_label: null,
      });
    } finally {
      useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    }
  });

  it("holds a block Generate requested during a pending Accept, then sends it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "extract_titles", prompt: "read titles" });
    });
    const generateSent = () =>
      streamCalls.some((call) => call.body.message.includes("extract_titles"));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    // A turn started now could stage a proposal the pending Accept then clears.
    expect(generateSent()).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(generateSent()).toBe(true));
  });

  it("ignores a reload retry that lands after a newer turn in the same chat", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    // The held reload will answer with a row that no longer has a proposal.
    let finishOldReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOldReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    await submit("rebuild it from scratch");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    await act(async () => {
      streamCalls[2]!.onMessage(
        proposalResponse("Rebuilt draft ready.", {
          turn_id: "turn-3",
          narrative_payload: proposalNarrativePayload({ turnId: "turn-3" }),
        }),
      );
      streamCalls[2]!.resolve();
    });
    await act(async () => {
      finishOldReload();
    });

    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("interrupted_draft reload shows an actionable candidate and its exact run result", async () => {
    // A hard kill can leave only the submitted user row. MessageItem does not
    // render footers on user bubbles, so the durable candidate needs a
    // standalone review gate until an assistant-owned row exists.
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "build the candidate",
        created_at: "2026-09-08T12:00:00Z",
      },
    ];
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Recovered draft",
    });
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-interrupted",
      revision: 3,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-exact",
    };
    historyResponse.data.proposed_workflow_run = {
      workflow_run_id: "wr-exact",
      status: "completed",
      available: true,
      failure_reason: null,
      outputs: [{ output_parameter_id: "op-metric", value: { metric: "42" } }],
    };

    await renderChat();

    expect(await screen.findByText("Associated test: completed")).toBeTruthy();
    const facts = screen.getByTestId("proposal-run-facts");
    expect(facts.textContent).toContain("op-metric:");
    expect(facts.textContent).toContain("metric: 42");
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  const objectOutputRunFacts = () => ({
    workflow_run_id: "wr-extracted",
    status: "completed",
    available: true,
    failure_reason: null,
    outputs: [
      {
        output_parameter_id: "op_extracted_data",
        value: {
          signed_in_url: "https://example.com/usage",
          visible_page_text: "Console\nSelect a project\nSearch...\nCtrl K",
        },
      },
    ],
  });

  const expectReadableObjectOutput = (facts: HTMLElement) => {
    expect(facts.textContent).toContain("Associated test: completed");
    expect(facts.textContent).toContain("signed_in_url:");
    expect(facts.textContent).toContain("https://example.com/usage");
    expect(facts.textContent).toContain("visible_page_text:");
    expect(facts.textContent).toContain("Select a project");
    expect(facts.textContent).not.toContain('{"');
    expect(facts.textContent).not.toContain("\\n");
    expect(within(facts).getByText(/Select a project/).className).toContain(
      "whitespace-pre-wrap",
    );
  };

  it("reload renders an object-valued test output as labelled values, not raw JSON", async () => {
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "log into the dashboard",
        created_at: "2026-09-08T12:00:00Z",
      },
    ];
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Extracted data",
    });
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-extracted",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-extracted",
    };
    historyResponse.data.proposed_workflow_run = objectOutputRunFacts();

    await renderChat();

    expect(await screen.findByText("Associated test: completed")).toBeTruthy();
    expectReadableObjectOutput(screen.getByTestId("proposal-run-facts"));
  });

  it("reload keeps the exact unavailable-run sentence", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-gone",
    };
    historyResponse.data.proposed_workflow_run = {
      workflow_run_id: "wr-gone",
      status: null,
      available: false,
      failure_reason: null,
      outputs: [],
    };

    await renderChat();

    expect((await screen.findByTestId("proposal-run-facts")).textContent).toBe(
      "Associated test run unavailable. No other run was substituted.",
    );
  });

  it("live terminal frame renders the same object-valued test output as a reload", async () => {
    await renderChat();
    await submit("log into the dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: "wr-extracted",
          },
          proposed_workflow_run: objectOutputRunFacts(),
        }),
      );
      streamCalls[0]!.resolve();
    });

    const live = screen.getByTestId("proposal-run-facts");
    expectReadableObjectOutput(live);
    const liveText = live.textContent;

    cleanup();
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "log into the dashboard",
        created_at: "2026-09-08T12:00:00Z",
      },
    ];
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-extracted",
    };
    historyResponse.data.proposed_workflow_run = objectOutputRunFacts();
    await renderChat();

    expect((await screen.findByTestId("proposal-run-facts")).textContent).toBe(
      liveText,
    );
  });

  it("live terminal frame with a run id but no facts backfills them from the chat row", async () => {
    await renderChat();
    await submit("log into the dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-extracted",
    };
    historyResponse.data.proposed_workflow_run = objectOutputRunFacts();
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: "wr-extracted",
          },
          proposed_workflow_run: null,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expectReadableObjectOutput(await screen.findByTestId("proposal-run-facts"));
  });

  it("leaves facts empty when the backfilled chat row names a different run", async () => {
    await renderChat();
    await submit("log into the dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-other",
    };
    historyResponse.data.proposed_workflow_run = {
      ...objectOutputRunFacts(),
      workflow_run_id: "wr-other",
    };
    const readsBeforeFrame = historyGet.mock.calls.length;
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: "wr-extracted",
          },
          proposed_workflow_run: null,
        }),
      );
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(historyGet.mock.calls.length).toBeGreaterThan(readsBeforeFrame),
    );
    expect(screen.queryByTestId("proposal-run-facts")).toBeNull();
  });

  it("backfills facts when a later turn tests a proposal an earlier turn owns", async () => {
    await renderChat();
    await submit("test it end to end");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-extracted",
    };
    historyResponse.data.proposed_workflow_run = objectOutputRunFacts();
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Test finished.", {
          turn_id: "turn-2",
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: "wr-extracted",
          },
          proposed_workflow_run: null,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expectReadableObjectOutput(await screen.findByTestId("proposal-run-facts"));
  });

  it("backfills facts on a first turn, before the chat id ref catches up", async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    await renderChat();
    await submit("log into the dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyResponse.data.workflow_copilot_chat_id = "chat-1";
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-extracted",
    };
    historyResponse.data.proposed_workflow_run = objectOutputRunFacts();
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: "wr-extracted",
          },
          proposed_workflow_run: null,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expect(historyGet).toHaveBeenCalledWith("/workflow/copilot/chat-history", {
      params: { workflow_copilot_chat_id: "chat-1" },
      timeout: 30_000,
      signal: expect.any(AbortSignal),
    });
    expectReadableObjectOutput(await screen.findByTestId("proposal-run-facts"));
  });

  it("restores an actionable gate via the chip after a bypassed proposal (old code: buttons vanish forever)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Always accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();

    // Bypass: send a follow-up instead of acting on the gate.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    // keep_pending_proposal must ride along on the bypassing request.
    expect(streamCalls[1]!.body.keep_pending_proposal).toBe(true);
    // Mid-flight: the gate's own actions are not accessible while loading.
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(
      screen.getByRole("button", { name: /1 proposal pending/ }),
    ).toBeTruthy();

    // Turn 2 ends with no new proposal; resync picks the row back up.
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    await waitFor(() => expect(historyGet).toHaveBeenCalled());

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    // The gate's owning turn is still not the last message, so the chip
    // (and its jump-back affordance) stays up even once actions re-enable.
    const chip = screen.getByRole("button", { name: /1 proposal pending/ });
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
        { timeout: 30_000, signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("shows auto-accept in the composer and Turn off mid-turn makes that turn's verified fix wait for review", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    const chip = await screen.findByRole("button", {
      name: /Auto-accepting/,
    });

    // The turn starts while auto-accept is still on.
    await submit("fix the selector");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });
    await act(async () => {
      fireEvent.click(chip);
    });
    // A failed write leaves the server auto-accepting, so the chip must not claim otherwise.
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();

    let finishTurnOff: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () => new Promise((resolve) => (finishTurnOff = resolve)),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/disable-auto-accept",
      { workflow_copilot_chat_id: "chat-1" },
    );
    historyResponse.data.auto_accept = false;

    // The server committed the write and finished the turn before the browser saw the POST resolve.
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Fixed and verified.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: false,
          narrative_payload: proposalNarrativePayload({
            proposalDisposition: "auto_applicable",
          }),
        }),
      );
      streamCalls[0]!.resolve();
    });
    // The gate offers no actions while Turn off is pending; the draft must still be waiting when it finishes.
    await act(async () => {
      finishTurnOff({});
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it.each([
    { rowKept: false, chipAfterAccept: "hidden" },
    // Apply writes auto-accept best-effort after creating the version, so the row can keep it on.
    { rowKept: true, chipAfterAccept: "shown" },
  ])(
    "after a plain Accept the chip follows the chat row (row kept auto_accept=$rowKept: $chipAfterAccept)",
    async ({ rowKept }) => {
      await renderChat();
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull();

      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      historyResponse.data.auto_accept = true;
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
      });
      expect(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      ).toBeTruthy();

      // Auto-accept never covers an untested draft, so this one still gets a gate.
      await submit("add a step");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
      await act(async () => {
        streamCalls[1]!.onMessage(
          proposalResponse("Untested draft.", {
            turn_id: "turn-2",
            narrative_payload: proposalNarrativePayload({
              turnId: "turn-2",
              turnIndex: 1,
            }),
          }),
        );
        streamCalls[1]!.resolve();
      });
      historyResponse.data.auto_accept = rowKept;
      await act(async () => {
        fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
      });

      expect(cancelPost).toHaveBeenLastCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.objectContaining({ auto_accept: false }),
        { timeout: 30_000, signal: expect.any(AbortSignal) },
      );
      await waitFor(() =>
        expect(
          Boolean(screen.queryByRole("button", { name: /Auto-accepting/ })),
        ).toBe(rowKept),
      );
    },
  );

  it("sends Turn off only after every in-flight Always accept settles, so auto-accept ends off", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    const finishApplies: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return new Promise((resolve) => {
          finishApplies.push((value) => {
            historyResponse.data.auto_accept = true;
            resolve(value);
          });
        });
      }
      if (path === "/workflow/copilot/disable-auto-accept") {
        historyResponse.data.auto_accept = false;
      }
      return Promise.resolve({});
    });
    const requestedPaths = () => cancelPost.mock.calls.map(([path]) => path);
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(finishApplies).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(requestedPaths()).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );

    await act(async () => {
      finishApplies[0]!({ data: proposedWorkflowPayload() });
    });
    await waitFor(() =>
      expect(requestedPaths()).toContain(
        "/workflow/copilot/disable-auto-accept",
      ),
    );
    expect(
      requestedPaths().lastIndexOf("/workflow/copilot/apply-proposed-workflow"),
    ).toBeLessThan(
      requestedPaths().indexOf("/workflow/copilot/disable-auto-accept"),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(historyResponse.data.auto_accept).toBe(false);
  });

  it("offers no Accept while Turn off is pending, so no Accept can start after it", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    let finishTurnOff: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => (finishTurnOff = resolve))
        : Promise.resolve({}),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });

    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    // Neither writes auto_accept, so the user keeps them while Turn off runs.
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();

    await act(async () => {
      finishTurnOff({});
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("gives the gate its Accept back when an Accept never settles, instead of waiting forever", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    // An apply that never answers: without a ceiling the chat's gate would stay without Accept.
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise(() => {})
        : Promise.resolve({}),
    );
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ACCEPT_SETTLE_CEILING_MS + 1_000);
    });
    vi.useRealTimers();

    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    // The disable never went out, so the chip still reports the row's state.
    expect(cancelPost.mock.calls.map(([path]) => path)).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });

  it("does not leak an Always accept into a New chat opened before it lands", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise((resolve) => (finishApply = resolve))
        : Promise.resolve({}),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });

    // New chat clears the chat and its auto-accept while that apply is still in flight.
    historyResponse.data.auto_accept = false;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      finishApply({ data: { workflow_id: "wf_proposed" } });
    });

    // The apply landed for a chat the user already left: it must not arm the blank chat.
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();

    // And it stays off once the blank chat's own turn assigns it a chat id to read back.
    await submit("start over");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Renamed the blocks.", {
          turn_id: "turn-2",
        }),
      );
      streamCalls[1]!.resolve();
    });

    expect(await screen.findByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

  it("ignores a read that started before a plain Accept turned auto-accept off", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByRole("button", { name: /Auto-accepting/ }),
    ).toBeTruthy();

    // The first Accept releases its reservation while its best-effort row read is still pending.
    let finishStaleRead: (value: unknown) => void = () => {};
    historyGet.mockImplementationOnce(
      () => new Promise((resolve) => (finishStaleRead = resolve)),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();

    await submit("add another step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Another untested draft.", {
          turn_id: "turn-2",
          narrative_payload: proposalNarrativePayload({
            turnId: "turn-2",
            turnIndex: 1,
          }),
        }),
      );
      streamCalls[1]!.resolve();
    });

    // A later plain Accept writes false before the earlier read returns its stale true value.
    historyResponse.data.auto_accept = false;
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );

    await act(async () => {
      finishStaleRead({ data: { ...historyResponse.data, auto_accept: true } });
    });

    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

  it("keeps Accept withheld until every Turn off for the chat settles, not just the first", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    const heldDisables: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => heldDisables.push(resolve))
        : Promise.resolve({}),
    );

    // Two clicks in one tick, before React re-renders: both start, as a double click does.
    const chip = await screen.findByRole("button", {
      name: /Auto-accepting/,
    });
    await act(async () => {
      fireEvent.click(chip);
      fireEvent.click(chip);
    });
    expect(heldDisables).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    // The first settles while the second is still in flight: the gate must stay withheld.
    await act(async () => {
      heldDisables[0]!({});
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();

    await act(async () => {
      heldDisables[1]!({});
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
  });

  it("gives the gate its Accept back when Turn off fails", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    let failTurnOff: (reason: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((_resolve, reject) => (failTurnOff = reject))
        : Promise.resolve({}),
    );

    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      );
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    await act(async () => {
      failTurnOff({ response: { status: 500 } });
    });

    // The request failed, so auto-accept is still on and the gate must be usable again.
    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();
  });

  it("keeps Turn off behind the accept's row write", async () => {
    // CHANGED BY THE #17099 MERGE. This asserted the same ordering against the CLIENT-SIDE
    // FALLBACK's row write (a v1 proposal whose apply 500s, applied locally and cleared by the
    // client). Slice 1 removes that fallback, so the vehicle is gone - but the concern is not:
    // the apply POST itself carries `auto_accept`, so it is still a write that can overwrite a
    // Turn off sent while it is in flight. Re-pointed at that write; the ordering it guards, and
    // the acceptsInFlight chain that enforces it, are unchanged.
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishRowWrite: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return new Promise((resolve) => (finishRowWrite = resolve));
      }
      return Promise.resolve({});
    });
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });

    // That write ends with auto_accept=true, so a Turn off sent first is silently overwritten.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(cancelPost.mock.calls.map(([path]) => path)).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );

    await act(async () => {
      finishRowWrite({
        data: proposedWorkflowPayload({ workflow_id: "wf_saved" }),
      });
    });
    await waitFor(() =>
      expect(cancelPost.mock.calls.map(([path]) => path)).toContain(
        "/workflow/copilot/disable-auto-accept",
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
  });

  it("ignores a read-back that started before Turn off finished", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    let finishStaleRead: (value: unknown) => void = () => {};
    historyGet.mockImplementationOnce(
      () => new Promise((resolve) => (finishStaleRead = resolve)),
    );
    // A plain Accept reads the row back; that read captures auto_accept=true before Turn off lands.
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );

    await act(async () => {
      finishStaleRead({ data: { ...historyResponse.data, auto_accept: true } });
    });

    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

  it("shows the Untested pill on hydration of an old payload lacking proposalDisposition", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      _copilot_unvalidated: true,
    });
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Here is a draft.",
        created_at: "2026-07-09T00:00:05Z",
        narrative_payload: null,
        turn_outcome: null,
      },
    ];
    await renderChat();

    await waitFor(() => expect(screen.getByText("Untested")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("renders a legacy (no-narrative) pending turn as pill + footer, no changes body", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      _copilot_unvalidated: true,
    });
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Here is a draft.",
        created_at: "2026-07-09T00:00:05Z",
        narrative_payload: null,
        turn_outcome: null,
      },
    ];
    await renderChat();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    expect(screen.getByText("Proposed changes")).toBeTruthy();
    expect(screen.queryByText("Added")).toBeNull();
    expect(screen.queryByText("Removed")).toBeNull();
    // No narrative turn id to derive an owning turn from — the chip would be
    // a dead no-op affordance here, not a working jump-back.
    expect(
      screen.queryByRole("button", { name: /1 proposal pending/ }),
    ).toBeNull();
  });

  it("clears a stale pending gate when a later turn is auto-applied (stale Accept can't reapply over the newer canvas)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    // Always-accept echo: this follow-up comes back auto-applied instead of another gate.
    await submit("actually just fix the typo directly");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.keep_pending_proposal).toBe(true);

    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Fixed and applied.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
          turn_id: "turn-2",
          narrative_payload: proposalNarrativePayload({
            turnId: "turn-2",
            turnIndex: 1,
            proposalDisposition: "auto_applicable",
            terminalMessage: "Fixed and applied.",
            narrativeSummary: "Fixed and applied.",
          }),
        }),
      );
      streamCalls[1]!.resolve();
    });

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
    expect(
      screen.queryByRole("button", { name: /1 proposal pending/ }),
    ).toBeNull();
  });

  it("shows the gate live on a narrative-less turn, not only after a reload (SKY-14099)", async () => {
    await renderChat();

    await submit("rename the blocks.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(legacyProposalResponse("Renamed the blocks."));
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByText("Proposed changes")).toBeTruthy();
    // The gate itself still appears live (SKY-14099); with no turn behind it there are
    // no facts to project, so it carries no verdict pill either way.
    expect(screen.queryByText("Tested")).toBeNull();
  });

  it("clears the proposal and shows a discarded receipt on a late Reject", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy(),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/clear-proposed-workflow",
        expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
      ),
    );
    expect(
      screen.getByText("Discarded — canvas reverted to the previous version"),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("surfaces per-block coverage tags through the mounted chat, not only in an isolated card", async () => {
    await renderChat();

    await submit("edit one block after it passed.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft needs review.", {
          narrative_payload: {
            ...editOneOfTwoBundle,
            turnId: "turn-1",
          },
        }),
      );
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(screen.getByText("Different source")).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Draft needs review/ }),
    ).toBeNull();
    expect(screen.queryByText("Built and tested the workflow")).toBeNull();
  });

  it("keeps the newest structured response in its expanded reading state", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(
        screen.getByText("Here is a draft workflow for you to review."),
      ).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Draft needs review/ }),
    ).toBeNull();
  });

  // The terminal pass is what stops "Confirming…" sitting there ONCE IT IS REACHED, and it gets
  // there by ignoring the claim. Reaching it is a separate problem, pinned by `does not re-arm
  // against its own expired deadline when a superseded pass settles first` and the two `re-arms
  // for a Try again ...` tests: a gate replaced after the last re-arm ran gets no timer unless
  // the effect is keyed on the gate itself.
  //
  // CHECK THE NEXT TWO PARAGRAPHS AGAINST THE CODE BEFORE RELYING ON THEM; the PR body records
  // their history. No running count here on purpose - a number you must maintain is a claim you
  // must re-verify every round, and this one went stale twice.
  //
  // `keeps Save held when another writer holds a fresh claim at the terminal pass` and `still
  // holds Save at the terminal pass when a fresh claim has no surviving proposal` share the fresh
  // 300-second claim but are NOT a minimal pair: the first adds `proposed_workflow` AND
  // `proposed_workflow_metadata`, the second adds neither. They are not separated by MECHANISM
  // either. Both rows REPORT a lease, and `claimHoldDeadline` returns a deadline from that number
  // without ever consulting metadata - its metadata branch is reached only when the field is
  // ABSENT. So the renewed-claim hold is the same in both, and the second test holds by three
  // independent routes, staying green under mutations the first catches.
  //
  // The pair is not minimal BECAUSE THE MINIMAL PAIR DOES NOT EXIST. The obvious repair is to
  // make the two differ in one field; that is impossible rather than merely unwanted, so do not
  // add a third test for the missing corner. A CHAT-HISTORY ROW never reports metadata with a
  // null proposal - the scope that matters, because these tests drive history rows. There is
  // exactly ONE history producer: `WorkflowCopilotChatHistoryResponse` is constructed in a single
  // place, fed by `_history_proposal_state`, which returns non-null metadata on exactly one of its
  // four paths - and that path returns the proposal beside it. The other two producers of the
  // field are STREAM frames, which is why the stream is NOT so constrained: each takes metadata
  // from the STORED proposal while sending this turn's `updated_workflow`, so a reply-turn frame
  // can carry metadata beside a null workflow. The canonical-moved branch is the real shape of "a
  // fresh claim with no surviving proposal" - it reports the claim while returning no metadata,
  // deliberately, because hiding a live claim behind a hidden proposal is the lie that branch
  // exists to avoid. That is the state the second test covers.
  //
  // `releases Save at the terminal pass when an older instance omits the claim lease, which is
  // deliberate` reports the accepting claim WITHOUT its lease and asserts a release. Absent and
  // null are different answers there, and collapsing them into "no claim" is the coalescing
  // `claimHoldDeadline` exists to prevent.
  const armTerminalPassWithRenewedClaim = async () => {
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    // Held BEFORE the terminal pass, so a later release is that pass and not something earlier.
    await waitFor(() => expect(saveHeld()).toBe(true));
    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    return saveHeld;
  };

  it("ignores a superseded reconciliation response instead of releasing Save under a live claim", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "baseline",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    const reads: Array<(value: unknown) => void> = [];
    historyGet.mockImplementation(
      () => new Promise((resolve) => reads.push(resolve)),
    );
    cancelPost.mockRejectedValueOnce(new Error("Response lost"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(2);
    const stale = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: null,
      },
    };
    const live = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: 300,
        proposed_workflow_metadata: {
          ...historyResponse.data.proposed_workflow_metadata,
          disposition: "accepting",
        },
      },
    };
    await act(async () => {
      reads[1]!(live);
    });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => {
      reads[0]!(stale);
    });
    expect(apply).not.toHaveBeenCalled();
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(reads.length).toBeGreaterThan(2);
    expect(reads.length).toBeLessThan(6);
  });

  it("keeps bounded recovery polling when a superseded read settles first", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "baseline",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    const reads: Array<(value: unknown) => void> = [];
    historyGet.mockImplementation(
      () => new Promise((resolve) => reads.push(resolve)),
    );
    cancelPost.mockRejectedValueOnce(new Error("Response lost"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(2);
    const stale = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: null,
      },
    };
    const live = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: 300,
        proposed_workflow_metadata: {
          ...historyResponse.data.proposed_workflow_metadata,
          disposition: "accepting",
        },
      },
    };
    await act(async () => {
      reads[0]!(stale);
    });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => {
      reads[1]!(live);
    });
    expect(apply).not.toHaveBeenCalled();
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(reads.length).toBeGreaterThan(2);
    expect(reads.length).toBeLessThan(6);
  });

  it("continues recovery polling after Retry supersedes a stale read", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "baseline",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    const reads: Array<(value: unknown) => void> = [];
    historyGet.mockImplementation(
      () => new Promise((resolve) => reads.push(resolve)),
    );
    cancelPost.mockRejectedValueOnce(new Error("Response lost"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(2);
    const stale = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: null,
      },
    };
    const live = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: 300,
        proposed_workflow_metadata: {
          ...historyResponse.data.proposed_workflow_metadata,
          disposition: "accepting",
        },
      },
    };
    await act(async () => {
      reads[1]!(live);
    });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => {
      reads[0]!(stale);
    });
    expect(apply).not.toHaveBeenCalled();
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(reads.length).toBeGreaterThan(2);
    expect(reads.length).toBeLessThan(6);
  });

  it("continues recovery polling when a stale read precedes the retry response", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "baseline",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    const reads: Array<(value: unknown) => void> = [];
    historyGet.mockImplementation(
      () => new Promise((resolve) => reads.push(resolve)),
    );
    cancelPost.mockRejectedValueOnce(new Error("Response lost"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reads).toHaveLength(2);
    const stale = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: null,
      },
    };
    const live = {
      data: {
        ...historyResponse.data,
        proposed_claim_expires_in_seconds: 300,
        proposed_workflow_metadata: {
          ...historyResponse.data.proposed_workflow_metadata,
          disposition: "accepting",
        },
      },
    };
    await act(async () => {
      reads[0]!(stale);
    });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => {
      reads[1]!(live);
    });
    expect(apply).not.toHaveBeenCalled();
    expect(screen.queryByText("Not saved")).toBeNull();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(reads.length).toBeGreaterThan(2);
    expect(reads.length).toBeLessThan(6);
  });

  it.each(["pending question", "hydrated Accept"])(
    "serializes question and Accept controls when the owner is %s",
    async (owner) => {
      // `handleQuestionAnswer` is fenced and returns false SILENTLY - no request, no toast. So a card
      // that stays clickable under an unresolved Accept reports a submit that never happened, which
      // is the false receipt this whole slice exists to remove, reached without the server at all.
      // The Cancel control beside it already gates on the same fence; this card was the outlier.
      historyResponse.data.question_interactions = [
        {
          interaction_id: "q-1",
          turn_id: "turn-1",
          tool_call_id: "ask-1",
          status: "pending",
          response: null,
          created_at: "2026-09-17T00:00:01Z",
          resolved_at: null,
          parts: [
            {
              part_id: "p-1",
              prompt: "Which one?",
              choices: [{ choice_id: "a", text: "Column A" }],
            },
          ],
        },
      ];
      // The proposal arrives by HYDRATION rather than the composer: a question card renders its own
      // textarea, and the shared `submit` helper resolves a single textbox by role.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "review_untested",
        workflow_run_id: null,
      };
      const view = await renderChat();

      // SETUP ASSERTION: the card is actionable BEFORE the fence closes. Without this the assertion
      // below passes for a card that was never enabled, or never rendered at all.
      const skip = await screen.findByRole("button", { name: "Skip" });
      expect(skip.matches(":disabled")).toBe(false);

      if (owner === "hydrated Accept") {
        view.unmount();
        historyResponse.data.proposed_workflow_metadata!.disposition =
          "accepting";
        historyResponse.data.proposed_claim_expires_in_seconds = 300;
        await renderChat();
        expect(await screen.findByText("Confirming…")).toBeTruthy();
        const blockedSkip = screen.getByRole("button", { name: "Skip" });
        expect(blockedSkip.matches(":disabled")).toBe(true);
        const actionRow = blockedSkip.parentElement!.parentElement!;
        expect(
          within(actionRow)
            .getByRole("button", { name: "Send" })
            .matches(":disabled"),
        ).toBe(true);
        expect(within(actionRow).queryByText(/choices selected/)).toBeNull();
        return;
      }
      historyResponse.data.proposed_claim_expires_in_seconds = 300;
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(cancelPost).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(
        screen.getByRole("button", { name: "Skip" }).matches(":disabled"),
      ).toBe(false);
    },
  );

  it("prevents a later saved-result path from bypassing unresolved Accept", async () => {
    const editorAccepts = true;
    const onWorkflowUpdate = vi.fn(() => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    const sends = postStreaming.mock.calls.length;
    await submit("another change");
    expect(postStreaming).toHaveBeenCalledTimes(sends);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("locks the continuation cards while an Accept is unresolved", async () => {
    // Same class as the question card, one hop further: `handleSend` returns silently when the
    // fence is up, so a Confirm click under an unresolved Accept reports an action that never
    // happened - a false receipt with no server involved. Reported by Codex and by a human
    // reviewer independently. The human also described the account picker as greying out for the
    // turn; that half does not hold, because `handleSend`'s early return RESOLVES and the
    // `.finally` clears the latch. The silent no-op is the real defect.
    // The pending proposal arrives by HYDRATION so a later reply turn cannot clear it - the
    // coexistence the finding describes: an older proposal still pending while the newest turn
    // ends in a confirmation request.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();
    await submit("and one more thing");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        plainReplyResponse("Ready - confirm and I'll apply the change.", {
          updated_workflow:
            proposedWorkflowPayload() as unknown as WorkflowApiResponse,
          proposal_disposition: "review_untested",
        }),
      );
      streamCalls[0]!.resolve();
    });

    // SETUP ASSERTION: actionable BEFORE the fence closes, so a green below cannot come from a
    // card that never rendered or was always inert.
    const confirm = await screen.findByRole("button", { name: "Confirm" });
    expect(confirm.matches(":disabled")).toBe(false);

    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming\u2026")).toBeTruthy();

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Confirm" }).matches(":disabled"),
      ).toBe(true),
    );
    // And the hold names itself on the card, not only in a title a disabled button may never fire.
    expect(
      screen.getAllByText(/Copilot is checking|may have saved/i).length,
    ).toBeGreaterThan(0);
  });

  it("names the unsaved canvas edits that a saved-gate retry would discard", async () => {
    // `saved`'s Try again REPLACES the canvas with the workflow the server confirmed. The card
    // already said "this replaces what's on the canvas" generically; it said the same thing when
    // there was nothing to lose and when there was. The editor already tracks the difference -
    // `hasChanges`, guarded by beginInternalUpdate so our own applies do not set it - and
    // `reconcileCanonicalWorkflow` already refuses to overwrite on exactly that signal. This card
    // names the cost while the work still exists.
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("editor could not load it");
    });
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    // NEGATIVE ARM: a clean canvas has nothing to lose, so the card must not invent a cost.
    await act(async () => {
      useWorkflowHasChangesStore.setState({ hasChanges: false });
    });
    // Scoped to the ADDED sentence: the card's base copy already ends with "...which discards
    // unsaved canvas edits", so a loose match here would assert against the wrong text.
    expect(screen.queryByText(/canvas has unsaved changes/i)).toBeNull();

    // POSITIVE ARM: the user repaired something under the fence. Now it has to say so.
    await act(async () => {
      useWorkflowHasChangesStore.setState({ hasChanges: true });
    });
    expect(await screen.findByText(/canvas has unsaved changes/i)).toBeTruthy();
  });

  it("does not let a stale reload retry resurrect the card a newer one cleared", async () => {
    // Try again on a `reload` with no attempt sets no `isAccepting` and disables nothing, so two
    // of these reads can be in flight. Only call identity separates them: the chat id and send
    // epoch this path already checks are identical across a double-click. Same guard, and the same
    // reason, as `reconcileFailedAccept`.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A follow-up turn with no new draft re-reads the row for the bypassed proposal; that read
    // fails, which is the only way this gate is reached.
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(screen.queryByText("Couldn't reload")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Two retries, both held open, released newest-first.
    let failOlder: () => void = () => {};
    let succeedNewer: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          failOlder = () => reject(new Error("older read lost"));
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    // The newer read SUCCEEDS and the proposal survives it. That matters: the reload card needs a
    // subject, so if the proposal were cleared here the card could not render whether or not the
    // stale catch restored the gate - and the assertion below would pass for the wrong reason.
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          succeedNewer = () =>
            resolve({
              data: {
                ...historyResponse.data,
                proposed_workflow: proposedWorkflowPayload(),
              },
            });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // SETUP ASSERTION: both reads really are in flight, or the ordering below proves nothing.
    expect(typeof failOlder).toBe("function");
    expect(typeof succeedNewer).toBe("function");

    await act(async () => {
      succeedNewer();
    });
    await waitFor(() =>
      expect(screen.queryByText("Couldn't reload")).toBeNull(),
    );

    // The older read now fails. Its catch must not restore a gate it no longer owns - that would
    // report a failure for a reload the user watched succeed, and hold Save behind it.
    await act(async () => {
      failOlder();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(saveHeld()).toBe(false);
  });

  const runOutTerminalPass = async () => {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
  };

  it("keeps Save held when another writer holds a fresh claim at the terminal pass", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    // Another writer took a full lease. Its proposal is still there because canonical has not
    // moved YET - that writer is mid-write, which is precisely when Save must not be released.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    await runOutTerminalPass();
    expect(saveHeld()).toBe(true);
  });

  it("keeps Save reserved when an older instance omits claim liveness", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = undefined;
    await runOutTerminalPass();
    expect(saveHeld()).toBe(true);
  });

  it("still holds Save at the terminal pass when a fresh claim has no surviving proposal", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    await runOutTerminalPass();
    expect(saveHeld()).toBe(true);
  });

  // Accept with no chat id yet: it resolves one mid-flight, and the ref that names it is
  // assigned by a passive effect, so the ref still reads empty for the rest of the handler.
  // `reconciles a failed Accept against the chat id it just resolved` and `holds the fence when
  // the chat row read comes back empty` both drive that window; they differ in what the row finds,
  // because
  // that is the only thing that tells the two halves of this fix apart.
  const acceptWithChatIdResolvedMidFlight = async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          workflow_copilot_chat_id: undefined,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    historyResponse.data.workflow_copilot_chat_id = "chat-1";
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
  };

  const saveIsHeld = () =>
    useWorkflowHasChangesStore.getState().saveBlockedReason !== null;

  it("reconciles a failed Accept against the chat id it just resolved", async () => {
    await acceptWithChatIdResolvedMidFlight();
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    await waitFor(() =>
      expect(historyGet).toHaveBeenCalledWith(
        "/workflow/copilot/chat-history",
        expect.objectContaining({
          params: { workflow_copilot_chat_id: "chat-1" },
        }),
      ),
    );
    expect(saveIsHeld()).toBe(true);
  });

  it("holds the fence when the chat row read comes back empty", async () => {
    await acceptWithChatIdResolvedMidFlight();
    // Resolve the id, then answer the row read with no row at all.
    historyGet.mockImplementationOnce(() => Promise.resolve(historyResponse));
    historyGet.mockImplementationOnce(() => Promise.resolve({ data: null }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    // Nothing identified the outcome, and this handler cleared its own gate before the apply,
    // so it owes a replacement. Unknown may not release Save.
    await waitFor(() => expect(saveIsHeld()).toBe(true));
  });

  it("keeps Save held when a reload's own fence reaches its terminal pass", async () => {
    // A reload lands on a claim with almost no lease left, so the fence hydration arms schedules
    // a TERMINAL recheck immediately - the first one, with no earlier read behind it. That is
    // the only path to the terminal pass that reconciliation never touches.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await waitFor(() => expect(saveHeld()).toBe(true));

    // Another writer takes a fresh full lease before that terminal read runs.
    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
    expect(saveHeld()).toBe(true);
  });

  it("holds Save when a terminal pass finds a live claim it has no baseline for", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A failed Accept arms the fence against a claim of ours.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 1.5;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // Try again finds NO claim: ours resolved, so the remembered remainder is gone with it.
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // Another writer then takes a lease before the terminal pass. It is SMALLER than the one
    // this fence first saw, so a comparison against a stale remainder would read it as our own
    // claim decaying and release Save over an active writer.
    //
    // The row carries METADATA on purpose: without it the claim is unattributable and the
    // WORKFLOW-level Save hold arms instead, which would hold Save for a different reason and
    // leave this test green whatever the renewal rule does. It has to be a claim the row can
    // tie to a proposal, so the only thing that can hold Save here is the renewal check.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2500));
    });
    expect(saveHeld()).toBe(true);
  });

  it("holds Save when a Reject-409 resync finds a claim it cannot attribute", async () => {
    // The unattributed-claim hold is a fact about the WORKFLOW, so it cannot live on the
    // hydration path alone: this row is learned through the resync a 409 drives, and another
    // writer holding the workflow is just as true when that is how we found out.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    expect(saveHeld()).toBe(false);

    // A concurrent save moved canonical, so the proposal is withheld - but its claim is live,
    // and the server reports it independently now.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    await waitFor(() => expect(saveHeld()).toBe(true));
  });

  it("keeps the baseline a renewal is measured against when an ordinary poll sees one", async () => {
    // Every other renewal test enters at the TERMINAL pass. This one drives the renewal arriving
    // on an ORDINARY re-check, which re-arms the fence - and a re-arm that adopted the new
    // reading as its baseline would leave the terminal pass comparing that writer's own lease
    // against itself, finding it smaller as it decays, and releasing Save under an active write.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 1.2;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // An ordinary re-check finds a LARGER remainder: our claim lapsed and another writer took
    // one. Try again is exactly that re-check - it re-arms the fence without being terminal.
    historyResponse.data.proposed_claim_expires_in_seconds = 2;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // The terminal pass then sees that writer's lease part-way through decaying, with its
    // proposal still visible because canonical has not moved yet.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 1.6;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 3000));
    });
    expect(saveHeld()).toBe(true);
  }, 15000);

  it("holds when a claim reappears after a read that reported none", async () => {
    // A read reporting NO claim is positive evidence that the lease ended - not absence of
    // information. Any claim seen afterwards was taken later, so with ABSOLUTE expiries it
    // cannot be the old one decaying: a lease is a fixed length added to claimed_at, so a newer
    // claim always expires later. A RELATIVE remainder cannot express that - a smaller number
    // read later looks like decay - which is why this sequence releases Save under it.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = null;
    const oursEndsAt = leaseDecrementingFrom(1.2);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // A re-check finds NO claim at all: ours is provably over.
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    const heldAfterNullRead = saveHeld();

    // Another writer then claims. Its remainder is SMALLER than the one this fence opened with,
    // so a relative comparison reads it as our own lease decaying.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    // A LEASE IS A FIXED LENGTH added to claimed_at, so a claim taken LATER always expires
    // later - even though its REMAINDER read at any instant is smaller than ours was at ours.
    // That is the whole of the distinction: 0.5s past our expiry, and a smaller remainder.
    leaseDecrementingFrom((oursEndsAt + 500 - Date.now()) / 1000);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2500));
    });
    expect(heldAfterNullRead).toBe(true);
    expect(saveHeld()).toBe(true);
  }, 15000);

  it("holds Save when mount hydration finds a claim it cannot attribute", async () => {
    // The THIRD row-learning site. Reconciliation and the Reject-409 resync are driven
    // elsewhere; this one is hydration, and removing its recordClaimState call left the whole
    // file green before this test existed.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().saveBlockedReason !== null,
      ).toBe(true),
    );
  });

  it("holds Save after an unreadable canonical read and a later live claim", async () => {
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? Promise.reject(new Error("Canonical read unavailable"))
        : Promise.resolve(historyResponse),
    );
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    vi.useFakeTimers();
    try {
      const openedAt = Date.now();
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = 180;
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(saveHeld()).toBe(true);
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(Date.now()).toBe(openedAt + 10_000);
      expect(saveHeld()).toBe(true);
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting",
        workflow_run_id: null,
      };
      historyResponse.data.proposed_claim_expires_in_seconds = 100;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(saveHeld()).toBe(true);
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      for (let poll = 0; poll < 9; poll += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000);
        });
      }
      expect(saveHeld()).toBe(true);
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting",
        workflow_run_id: null,
      };
      historyResponse.data.proposed_claim_expires_in_seconds = 90;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(historyGet.mock.calls.length).toBeGreaterThan(3);
      expect(screen.getByText("Confirming…")).toBeTruthy();
      expect(saveHeld()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  }, 15_000);

  // CALLBACK-LEVEL, deliberately. These drive onSelectChat directly, which is where the guard
  // resolves. Whether today's DOM lets a user reach the overlapping window is NOT established -
  // WorkflowCopilotHistory intends to disable and close once loadChatInPlace sets
  // isLoadingHistory, and the docked store is updated by a passive effect, so the window is
  // bounded by that effect. These assert the guard's resolution, not a user-reachable path.
  const selectChat = async (id: string) => {
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: id,
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });
  };

  const chatPayload = (id: string, text: string) => ({
    data: {
      ...historyResponse.data,
      workflow_copilot_chat_id: id,
      proposed_workflow: null,
      chat_history: [
        {
          sender: "ai",
          content: text,
          created_at: new Date().toISOString(),
        },
      ],
    },
  });

  it("lets the NEWER chat selection win when two loads overlap", async () => {
    await renderChat({ docked: true });
    let releaseSecond: ((value: unknown) => void) | null = null;
    let releaseThird: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseSecond = resolve as (value: unknown) => void;
        }),
    );
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseThird = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    await selectChat("chat-3");
    expect(releaseSecond).not.toBeNull();
    expect(releaseThird).not.toBeNull();

    // The older load returns FIRST, then the newer one.
    await act(async () => {
      releaseSecond!(chatPayload("chat-2", "SECOND chat body"));
      await Promise.resolve();
    });
    await act(async () => {
      releaseThird!(chatPayload("chat-3", "THIRD chat body"));
      await Promise.resolve();
    });

    // The user's most recent selection is what they get.
    await waitFor(() =>
      expect(screen.queryByText("THIRD chat body")).not.toBeNull(),
    );
    expect(screen.queryByText("SECOND chat body")).toBeNull();
  });

  it("lets a re-selection of the CURRENT chat cancel a pending load", async () => {
    // The equality early return makes no second read, so an increment placed after it would
    // never run for this case - and this is the case where a user undoes a mis-click.
    await renderChat({ docked: true });
    let releaseOther: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseOther = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    expect(releaseOther).not.toBeNull();
    await selectChat("chat-1");

    await act(async () => {
      releaseOther!(chatPayload("chat-2", "OTHER chat body"));
      await Promise.resolve();
    });
    expect(screen.queryByText("OTHER chat body")).toBeNull();
  });

  it("attaches the auto-commit saved gate to the committing turn, not the bypassed one", async () => {
    // From reviewer 1677. POSITIVE assertion on purpose: "the status is not inside turn 1's gate"
    // passes when the element is absent, when the render changed shape, and when turn 2 never
    // rendered a gate at all - an earlier attempt of mine did exactly that and pinned nothing.
    // Walking up from the status node and naming the host gate says what IS true.
    //
    // PARTIAL PIN, and the limit is worth knowing: it fails when the turn-id handle survives, and
    // it also passes when only `setProposedWorkflow(null)` is dropped. It pins the gate's
    // ATTACHMENT, not the staged draft handle.
    const onWorkflowUpdate = vi.fn((workflow: { workflow_id?: string }) => {
      if (workflow?.workflow_id === "wf_turn2")
        throw new Error("editor could not load it");
    });
    await renderChat({
      onWorkflowUpdate:
        onWorkflowUpdate as unknown as ChatProps["onWorkflowUpdate"],
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("now change it again");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Committed.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
          updated_workflow: proposedWorkflowPayload({
            workflow_id: "wf_turn2",
          }) as unknown as WorkflowApiResponse,
          narrative_payload: proposalNarrativePayload({
            turnId: "turn-2",
            turnIndex: 1,
            proposalDisposition: "auto_applicable",
          }),
        }),
      );
      streamCalls[1]!.resolve();
    });
    const status = await screen.findByText("Saved, not shown");
    expect(status.closest('[id^="copilot-gate-"]')?.id).toBe(
      "copilot-gate-turn-2",
    );
  });

  it("holds Save when the server committed a version the editor cannot take", async () => {
    // The auto-commit path ignored applyWorkflowUpdate's boolean: with `workflow_applied: true`
    // the server HAS written this version, so an editor that rejects it leaves the canvas older
    // than canonical - and clearing the proposal handles there released Save over it with
    // nothing on screen saying so. That is the state `saved` exists for, reached by auto-commit
    // instead of Accept, so it arms the same gate.
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("editor could not load it");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Committed.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(saveHeld()).toBe(true);
  });

  it("keeps the chat's controls locked while a newer load is still in flight", async () => {
    // The selection epoch stops a SUPERSEDED response before it applies - but its `finally`
    // cleared the loading flag anyway, and that flag gates History, New chat and the gate's
    // action row. So the abandoned load unlocked the chat while the newer one was still running,
    // and an Accept started in that window races a response that will swap the chat underneath
    // it. The same shape was in the mount loader's `finally` too; both now release through
    // endHistoryLoad, which ignores a load that no longer owns the flag.
    //
    // Callback level, like `lets the NEWER chat selection win when two loads overlap` and `lets a
    // re-selection of the CURRENT chat cancel a pending load`: this asserts the guard's
    // resolution. DOM reachability of the overlapping window is bounded by the passive effect
    // that publishes these controls and is NOT established.
    await renderChat({ docked: true });
    let releaseOlder: ((value: unknown) => void) | null = null;
    let releaseNewer: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseOlder = resolve as (value: unknown) => void;
        }),
    );
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseNewer = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    await selectChat("chat-3");
    expect(releaseOlder).not.toBeNull();
    expect(releaseNewer).not.toBeNull();

    // The superseded load answers first and must not unlock anything.
    await act(async () => {
      releaseOlder!(chatPayload("chat-2", "SECOND chat body"));
      await Promise.resolve();
    });
    expect(useCopilotHeaderStore.getState().controls?.disabled).toBe(true);

    // The load that still owns the flag releases it, so this does not latch.
    await act(async () => {
      releaseNewer!(chatPayload("chat-3", "THIRD chat body"));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(useCopilotHeaderStore.getState().controls?.disabled).toBe(false),
    );
  });

  it("blocks another chat from replacing the reserved chat's claim evidence", async () => {
    await renderChat({ docked: true });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      useWorkflowHasChangesStore.getState().saveBlockedReason !== null;
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));
    historyGet.mockImplementationOnce(() =>
      Promise.resolve({
        data: {
          ...historyResponse.data,
          workflow_copilot_chat_id: "chat-2",
          proposed_workflow: null,
          proposed_workflow_metadata: null,
          proposed_claim_expires_in_seconds: null,
          chat_history: [
            {
              sender: "ai",
              content: "SECOND chat body",
              created_at: new Date().toISOString(),
            },
          ],
        },
      }),
    );
    await selectChat("chat-2");
    expect(screen.queryByText("SECOND chat body")).toBeNull();
    expect(saveHeld()).toBe(true);
    expect(useCopilotHeaderStore.getState().controls?.disabled).toBe(true);
  });

  it("keeps the History lock reason reachable on the floating header", async () => {
    // The floating header is outside any TooltipProvider, so the reason is a native title -
    // and a DISABLED button receives no hover and takes no focus, so a title ON IT can never
    // be read. It has to hang off something that is not disabled.
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // An Accept that never settles holds the fence, which is what locks History.
    cancelPost.mockImplementationOnce(() => new Promise(() => {}));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    const history = screen.getByRole("button", { name: /^History/ });
    expect(history.matches(":disabled")).toBe(true);
    const explained = screen
      .getAllByTitle("Copilot is saving your accepted changes.")
      .find((el) => el.contains(history))!;
    expect(explained).toBeTruthy();
    // Whatever carries the reason must be able to receive a hover: not the disabled control.
    expect(explained.matches(":disabled")).toBe(false);
    expect(explained.contains(history)).toBe(true);
  });

  it("retains and resyncs a typed proposal when atomic Accept fails", async () => {
    changesState.hasChanges = true;
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("records a late Accept for the unmounted workflow and keeps the next workflow snapshot clean", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn((workflow: WorkflowApiResponse) => {
      useWorkflowTitleStore.getState().setTitle(workflow.title);
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromWorkflow(workflow.description);
      useWorkflowParametersStore.getState().setParameters([]);
    });
    const view = await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveAccept!: (response: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAccept = resolve;
        }),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.anything(),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();

    view.unmount();
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts.wpid_1,
    ).toBeDefined();
    if (useWorkflowYamlEditorStore.getState().editorOwner)
      unregisterEditorOwner(useWorkflowYamlEditorStore.getState().editorOwner!);
    registerEditorOwner(createYamlCommitOwner("wpid_2"));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    const parameter = {
      key: "next_input",
      parameterType: "context" as const,
      sourceParameterKey: "source",
    };
    useWorkflowTitleStore.getState().setTitle("Next workflow");
    useWorkflowTitleStore
      .getState()
      .setDescriptionFromWorkflow("Next description");
    useWorkflowParametersStore.getState().setParameters([parameter]);
    expect(useWorkflowTitleStore.getState().title).toBe("Next workflow");
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      parameter,
    ]);
    const titleState = useWorkflowTitleStore.getState();
    const parametersState = useWorkflowParametersStore.getState();
    useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
      ...saveData,
      workflow: { ...saveData.workflow, workflow_permanent_id: "wpid_2" },
    }));
    useWorkflowHasChangesStore.getState().setHasChanges(false);
    const snapshot = captureEditorState({
      workflowPermanentId: "wpid_2",
      nodes: editorNodes,
      edges: [],
      parameters: parametersState.parameters,
      title: titleState.title,
      titleHasBeenGenerated: titleState.titleHasBeenGenerated,
      description: titleState.description,
      hasChanges: false,
      saveGeneration: useWorkflowHasChangesStore.getState().saveGeneration,
    });

    await act(async () =>
      resolveAccept({
        data: proposedWorkflowPayload({ description: "Late description" }),
      }),
    );
    expect(apply).not.toHaveBeenCalled();
    expect(useWorkflowTitleStore.getState()).toBe(titleState);
    expect(useWorkflowParametersStore.getState()).toBe(parametersState);
    expect(
      useWorkflowHasChangesStore.getState().saveGenerationsByWorkflow,
    ).toEqual({ wpid_1: snapshot.saveGeneration + 1 });
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    expect(
      restoreEditorState(snapshot, {
        workflowPermanentId: "wpid_2",
        setNodes: setEditorNodes,
        setEdges: vi.fn(),
        parametersStore: useWorkflowParametersStore.getState(),
        titleStore: useWorkflowTitleStore.getState(),
        changesStore: useWorkflowHasChangesStore.getState(),
        collapseStore: useNodeCollapseStore.getState(),
        restoreOwnership: (workflowPermanentId) => {
          useWorkflowParametersStore.setState({
            parametersWorkflowPermanentId: workflowPermanentId,
          });
          useWorkflowTitleStore.setState({
            titleWorkflowPermanentId: workflowPermanentId,
            descriptionWorkflowPermanentId: workflowPermanentId,
          });
        },
        scheduleLayout: vi.fn(),
        isLockedByOther,
      }),
    ).toBe("restored");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().lockKind).toBeNull();
  });

  describe.each(["editor", "debugger"] as const)(
    "late canonical recovery on the %s route",
    (route) => {
      beforeEach(() => {
        saveData.workflow.title = saveData.title;
      });
      async function discardRecoveryDraft() {
        await act(async () => {
          fireEvent.click(
            screen.getByRole("button", { name: "Apply and discard my edits" }),
          );
          await vi.advanceTimersByTimeAsync(0);
        });
      }
      async function severStream(withTurnId = true) {
        const apply = vi.fn();
        const view = await renderChat({
          route,
          docked: true,
          onWorkflowUpdate: apply,
        });
        const saveRegistration = renderHook(() => {
          const nodes = editorNodes;
          const revision = useWorkflowYamlEditorStore(
            (state) => state.revision,
          );
          const parameters = useWorkflowParametersStore(
            (state) => state.parameters,
          );
          useEffect(() => {
            // FlowRenderer registers a new save callback after graph/store updates render.
            const current = {
              ...saveData,
              blocks: getWorkflowBlocks(nodes, []),
            };
            useWorkflowHasChangesStore.getState().setGetSaveData(() => current);
          }, [nodes, revision, parameters]);
        });
        const saved = { current: saveData.workflow };
        historyGet.mockImplementation((path: string) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: saved.current }
              : historyResponse,
          ),
        );
        const submittedBlocks = useWorkflowHasChangesStore
          .getState()
          .getSaveData()!.blocks;
        await submit("edit the workflow");
        vi.useFakeTimers();
        await act(async () => {
          if (withTurnId) {
            streamCalls[0]!.onMessage({
              type: "turn_start",
              turn_id: "turn-1",
              mode: "build",
              turn_index: 0,
            });
            streamCalls[0]!.onMessage({
              type: "workflow_draft",
              block_labels: [],
              workflow: proposedWorkflowPayload(),
            });
          }
        });
        if (withTurnId) {
          act(() => saveRegistration.rerender());
          expect(
            useWorkflowHasChangesStore.getState().getSaveData()!.blocks,
          ).not.toEqual(submittedBlocks);
        }
        await act(async () =>
          streamCalls[0]!.reject(new Error("connection dropped")),
        );
        expect(historyGet).toHaveBeenCalledWith(
          "/workflows/wpid_1",
          expect.objectContaining({ signal: expect.any(AbortSignal) }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(setEditorNodes).not.toHaveBeenCalled();
        if (withTurnId) {
          expect(
            editorNodes.find((node) => node.id === "loop"),
          ).toBeUndefined();
        } else {
          expect(
            editorNodes.find((node) => node.id === "loop")?.data,
          ).toMatchObject({
            loopValue: "unsaved_items",
            loopVariableReference: "{{ item }}",
          });
        }
        apply.mockClear();
        vi.mocked(toast).mockClear();
        return { apply, saved, view };
      }

      it.each([false, true])(
        "retains recovery through timeout and Retry until reconciliation or Reject (resumed=%s)",
        async (resumed) => {
          changesState.hasChanges = true;
          const { saved, view } = await severStream();
          if (resumed) {
            view.unmount();
            await renderChat({ route, docked: true, isOpen: false });
          }
          const expectControls = () => {
            expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
            expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
            expect(
              useWorkflowYamlEditorStore.getState().copilotAcceptance,
            ).not.toBeNull();
          };
          await advance(1_500_000);
          expectControls();
          expect(screen.getByRole("alert").textContent).toContain(
            "Could not confirm",
          );
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "Retry" })),
          );
          expectControls();
          const retainedNodes = structuredClone(editorNodes);
          act(editLoop);
          expect(editorNodes).toEqual(retainedNodes);
          finishOnServer(saved);
          await advance(2_000);
          if (resumed) await discardRecoveryDraft();
          expect(
            editorNodes.find((node) => node.id === "loop"),
          ).toBeUndefined();
          expect(screen.queryByRole("alert")).toBeNull();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
        },
      );

      it("keeps Reject reachable in a closed chat after an unchanged Retry and another timeout", async () => {
        changesState.hasChanges = true;
        const { view } = await severStream(false);
        view.unmount();
        const recovery = canonicalRecoveriesByWorkflow.get("wpid_1")!;
        const snapshot = recovery.rollback?.snapshot;
        await renderChat({ route, docked: true, isOpen: false });
        expect(screen.queryByRole("textbox")).toBeNull();
        expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
        await advance(1_500_000);
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Retry" })),
        );
        expect(recovery.rollback?.snapshot).toEqual(snapshot);
        await advance(1_500_000);
        historyGet.mockImplementation((path: string) =>
          path === "/workflows/wpid_1"
            ? new Promise(() => {})
            : Promise.resolve(historyResponse),
        );
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        expect(screen.getByRole("alert").textContent).toContain(
          "Cancelling the Copilot turn",
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(recovery.rollback?.snapshot).toEqual(snapshot);
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          {
            cancel_token: recovery.poll!.requestId,
            workflow_copilot_chat_id: "chat-1",
            source: "stop_button",
          },
          { timeout: 5_000 },
        );
        expect(
          editorNodes.find((node) => node.id === "loop")?.data,
        ).toMatchObject({ loopValue: "unsaved_items" });
      });

      it.each(["poll", "resume", "retry"] as const)(
        "bounds a hung canonical read from %s and ignores its stale response",
        async (source) => {
          changesState.hasChanges = true;
          const { saved, view } = await severStream(false);
          historyResponse.data.request_turn_id = "turn-1";
          finishTurnHistory();
          let resolveOldRead!: (value: unknown) => void;
          let readSignal: AbortSignal | undefined;
          historyGet.mockImplementation(
            (path: string, options?: { signal?: AbortSignal }) =>
              path === "/workflows/wpid_1"
                ? new Promise((resolve) => {
                    if (!readSignal) {
                      readSignal = options?.signal;
                      resolveOldRead = resolve;
                    }
                  })
                : Promise.resolve(historyResponse),
          );
          if (source === "resume") {
            view.unmount();
            await renderChat({ route, docked: true });
          } else if (source === "retry") {
            await act(async () =>
              fireEvent.click(screen.getByRole("button", { name: "Retry" })),
            );
          }
          await advance(source === "retry" ? 0 : 2_000);
          expect(readSignal?.aborted).toBe(false);
          await advance(4_999);
          expect(readSignal?.aborted).toBe(false);
          await advance(1);
          expect(readSignal?.aborted).toBe(true);
          await advance(1_500_000);
          expect(readSignal?.aborted).toBe(true);
          expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
          finishOnServer(saved);
          historyGet.mockImplementation((path: string) =>
            Promise.resolve(
              path === "/workflows/wpid_1"
                ? { data: saved.current }
                : historyResponse,
            ),
          );
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "Retry" })),
          );
          await advance(0);
          if (source === "resume") await discardRecoveryDraft();
          expect(
            editorNodes.find((node) => node.id === "loop"),
          ).toBeUndefined();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          await act(async () => resolveOldRead({ data: saveData.workflow }));
          expect(screen.queryByRole("alert")).toBeNull();
          expect(
            editorNodes.find((node) => node.id === "loop"),
          ).toBeUndefined();
        },
      );

      it("evicts the least recently visited detached recovery", async () => {
        changesState.hasChanges = true;
        const { view } = await severStream(false);
        view.unmount();
        const recovery = canonicalRecoveriesByWorkflow.get("wpid_1")!;
        for (let index = 2; index <= 20; index += 1) {
          canonicalRecoveriesByWorkflow.set(`wpid_${index}`, {
            ...recovery,
            workflowPermanentId: `wpid_${index}`,
          });
        }
        const revisit = await renderChat({
          route,
          docked: true,
          isOpen: false,
        });
        revisit.unmount();
        canonicalRecoveriesByWorkflow.set("wpid_21", {
          ...recovery,
          workflowPermanentId: "wpid_21",
        });
        const newest = await renderChat({
          route,
          docked: true,
          isOpen: false,
          workflowPermanentId: "wpid_21",
        });
        newest.unmount();
        expect(canonicalRecoveriesByWorkflow.size).toBe(20);
        expect(canonicalRecoveriesByWorkflow.has("wpid_2")).toBe(false);
        expect(canonicalRecoveriesByWorkflow.has("wpid_1")).toBe(true);
        expect(canonicalRecoveriesByWorkflow.has("wpid_21")).toBe(true);
      });

      function finishOnServer(saved: {
        current: WorkflowSaveData["workflow"];
      }) {
        historyResponse.data.request_turn_id = "turn-1";
        saved.current = {
          ...saveData.workflow,
          workflow_id: "wf_committed",
          version: 2,
          workflow_definition: { blocks: [], parameters: [] },
        };
        historyResponse.data.chat_history = [
          {
            sender: "ai",
            content: "Saved the workflow.",
            created_at: "2026-09-11T00:00:00Z",
            turn_outcome: {
              copilot_turn_id: "turn-1",
              terminal_reason: null,
            },
          },
        ];
      }

      async function advance(ms: number) {
        await act(async () => vi.advanceTimersByTimeAsync(ms));
      }

      function editLoop() {
        if (refuseMutationDuringYamlCommit()) return;
        editorNodes = editorNodes.map((node) =>
          node.id === "loop"
            ? ({
                ...node,
                data: { ...node.data, loopValue: "later_user_items" },
              } as AppNode)
            : node,
        );
        useWorkflowHasChangesStore.getState().setHasChanges(true);
      }

      it.each([
        { interruption: "stream drop", titleFrameReceived: true },
        { interruption: "navigation", titleFrameReceived: true },
        { interruption: "stream drop", titleFrameReceived: false },
        { interruption: "navigation", titleFrameReceived: false },
      ])(
        "waits for the terminal row after a title-only write and $interruption before releasing the editor (title frame received=$titleFrameReceived)",
        async ({ interruption, titleFrameReceived }) => {
          changesState.hasChanges = true;
          saveData.workflow.version = 1;
          saveData.workflow.modified_at = "2026-09-11T00:00:00Z";
          const apply = vi.fn();
          const view = await renderChat({
            route,
            docked: true,
            onWorkflowUpdate: apply,
          });
          await submit("edit the workflow");
          const call = streamCalls[0]!;
          vi.useFakeTimers();
          const saved = {
            current: {
              ...saveData.workflow,
              title: "Saved name",
              modified_at: "2026-09-11T00:00:01Z",
            },
          };
          historyGet.mockImplementation((path: string) =>
            Promise.resolve(
              path === "/workflows/wpid_1"
                ? { data: saved.current }
                : historyResponse,
            ),
          );
          await act(async () => {
            call.onMessage({
              type: "turn_start",
              turn_id: "turn-1",
              mode: "build",
              turn_index: 0,
            });
            if (titleFrameReceived) {
              call.onMessage({
                type: "title_update",
                workflow_permanent_id: "wpid_1",
                title: "Saved name",
              });
            }
          });
          if (interruption === "stream drop") {
            await act(async () => call.reject(new Error("connection dropped")));
          } else {
            await act(async () => view.unmount());
            expect(
              canonicalRecoveriesByWorkflow.get("wpid_1")?.rollback
                ?.titlePersisted,
            ).toBe(titleFrameReceived);
            expect(
              canonicalRecoveriesByWorkflow.get("wpid_1")?.rollback
                ?.workflowPersisted,
            ).toBe(false);
            await renderChat({ route, docked: true, onWorkflowUpdate: apply });
          }
          if (vi.isFakeTimers()) await advance(2_000);
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).not.toBeNull();
          const owner =
            useWorkflowYamlEditorStore.getState().editorOwner ??
            createYamlCommitOwner("wpid_1");
          registerEditorOwner(owner);
          const reservation =
            useWorkflowYamlEditorStore.getState().copilotAcceptance;
          expect(reservation).not.toBeNull();
          expect(beginSaveTransaction(owner)).toBe(false);
          expect(screen.getByRole("alert")).toBeTruthy();
          expect(apply).not.toHaveBeenCalled();

          if (interruption === "navigation") {
            await act(async () =>
              useCopilotHeaderStore.getState().controls!.onNewChat(),
            );
          }
          historyResponse.data.chat_history = [
            {
              sender: "ai",
              content: "Another turn finished.",
              created_at: "2026-09-11T00:00:02Z",
              turn_outcome: {
                copilot_turn_id: "turn-other",
                terminal_reason: null,
              },
            },
          ];
          await advance(2_000);
          expect(historyGet).toHaveBeenCalledWith(
            "/workflow/copilot/chat-history",
            expect.objectContaining({
              params: { workflow_copilot_chat_id: "chat-1" },
            }),
          );
          expect(apply).not.toHaveBeenCalled();
          expect(screen.getByRole("alert")).toBeTruthy();
          expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
            reservation,
          );

          historyGet.mockClear();
          finishOnServer(saved);
          await advance(5_000);
          expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
            "/workflow/copilot/chat-history",
            "/workflows/wpid_1",
          ]);
          if (interruption === "navigation") await discardRecoveryDraft();
          expect(apply).toHaveBeenCalledExactlyOnceWith(
            saved.current,
            expect.objectContaining({ persisted: true, applied: true }),
          );
          if (interruption === "navigation") {
            expect(
              useCopilotHeaderStore.getState().controls!.currentChatId,
            ).toBeNull();
            if (interruption === "navigation")
              expect(screen.queryByText("Saved the workflow.")).toBeNull();
            else expect(screen.getByText("Saved the workflow.")).toBeTruthy();
          }
          expect(screen.queryByRole("alert")).toBeNull();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          expect(beginSaveTransaction(owner)).toBe(true);
          expect(
            buildWorkflowSaveRequest({
              ...saveData,
              blocks: getWorkflowBlocks(editorNodes, []),
            }).workflow_definition.blocks,
          ).toEqual(saved.current.workflow_definition.blocks);
          finishSaveTransaction(owner);
          historyGet.mockClear();
          await advance(30_000);
          expect(historyGet).not.toHaveBeenCalled();
        },
      );

      it("retains the submitted draft and refuses edits until the late server workflow resolves", async () => {
        changesState.hasChanges = true;
        const { saved } = await severStream();
        const owner =
          useWorkflowYamlEditorStore.getState().editorOwner ??
          createYamlCommitOwner("wpid_1");
        registerEditorOwner(owner);
        expect(beginSaveTransaction(owner)).toBe(false);
        const retainedNodes = structuredClone(editorNodes);
        act(editLoop);
        expect(editorNodes).toEqual(retainedNodes);
        expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
        finishOnServer(saved);
        await advance(2_000);
        expect(editorNodes.find((node) => node.id === "loop")).toBeUndefined();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
        expect(beginSaveTransaction(owner)).toBe(true);
        finishSaveTransaction(owner);
      });

      it.each(["new chat", "history"] as const)(
        "continues canonical recovery after switching to %s without replacing the selected chat",
        async (destination) => {
          changesState.hasChanges = true;
          const { apply, saved } = await severStream();
          const controls = useCopilotHeaderStore.getState().controls!;
          const selectedHistory = {
            ...historyResponse.data,
            workflow_copilot_chat_id: "chat-other",
            chat_history: [
              {
                sender: "ai",
                content: "Selected history",
                created_at: "2026-09-11T00:00:00Z",
              },
            ],
          };
          historyGet.mockImplementation(
            (
              path: string,
              config?: { params?: { workflow_copilot_chat_id?: string } },
            ) =>
              Promise.resolve(
                path === "/workflows/wpid_1"
                  ? { data: saved.current }
                  : config?.params?.workflow_copilot_chat_id === "chat-other"
                    ? { data: selectedHistory }
                    : historyResponse,
              ),
          );
          expect(controls.disabled).toBe(false);
          await act(async () => {
            if (destination === "new chat") controls.onNewChat();
            else
              controls.onSelectChat({
                workflow_copilot_chat_id: "chat-other",
                workflow_permanent_id: "wpid_1",
                title: "Another chat",
                created_at: "2026-09-11T00:00:00Z",
                modified_at: "2026-09-11T00:00:00Z",
              });
          });
          const selectedChat = destination === "new chat" ? null : "chat-other";
          expect(useCopilotHeaderStore.getState().controls!.currentChatId).toBe(
            selectedChat,
          );
          historyGet.mockClear();
          finishOnServer(saved);
          await advance(2_000);
          expect(historyGet).toHaveBeenCalledWith(
            "/workflows/wpid_1",
            expect.objectContaining({ signal: expect.any(AbortSignal) }),
          );
          expect(apply).toHaveBeenCalledExactlyOnceWith(
            saved.current,
            expect.objectContaining({ persisted: true, applied: true }),
          );
          expect(useCopilotHeaderStore.getState().controls!.currentChatId).toBe(
            selectedChat,
          );
          expect(screen.queryByText("Saved the workflow.")).toBeNull();
          if (destination === "history")
            expect(screen.getByText("Selected history")).toBeTruthy();
          historyGet.mockClear();
          await advance(30_000);
          expect(historyGet).not.toHaveBeenCalled();
        },
      );

      it("refuses edits while the late canonical read owns the reservation", async () => {
        changesState.hasChanges = true;
        const { apply, saved } = await severStream();
        finishOnServer(saved);
        let resolveRead!: (value: unknown) => void;
        historyGet.mockImplementation((path: string) =>
          path === "/workflows/wpid_1"
            ? new Promise((resolve) => {
                resolveRead = resolve;
              })
            : Promise.resolve(historyResponse),
        );
        await advance(2_000);
        expect(resolveRead).toBeTypeOf("function");
        const retainedNodes = structuredClone(editorNodes);
        act(editLoop);
        expect(editorNodes).toEqual(retainedNodes);
        expect(toast).toHaveBeenCalledExactlyOnceWith({
          title: "Wait for the Copilot change to finish",
          variant: "destructive",
        });
        historyGet.mockImplementation((path: string) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: saved.current }
              : historyResponse,
          ),
        );
        await act(async () => resolveRead({ data: saved.current }));
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          saved.current,
          expect.objectContaining({ persisted: true, applied: true }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
      });

      it.each([false, true])(
        "parks a live turn on navigation and reconciles its commit before saving (turn started=%s)",
        async (turnStarted) => {
          changesState.hasChanges = true;
          const view = await renderChat({ route, docked: true });
          const queryKey = ["workflow", "wpid_1"];
          queryClient.setQueryData(queryKey, saveData.workflow);
          const submittedSnapshot = structuredClone(editorNodes);
          await submit("edit the workflow");
          const call = streamCalls[0]!;
          if (turnStarted) {
            await act(async () => {
              call.onMessage({
                type: "turn_start",
                turn_id: "turn-1",
                mode: "build",
                turn_index: 0,
              });
            });
          }
          await act(async () => view.unmount());
          const parked = canonicalRecoveriesByWorkflow.get("wpid_1");
          expect(parked).toMatchObject({
            resumed: true,
            baseline: saveData.workflow,
            preservedSettings: saveData.settings,
            poll: {
              chatId: "chat-1",
              turnId: turnStarted ? "turn-1" : null,
              requestId: expect.any(String),
            },
            rollback: { snapshot: { nodes: submittedSnapshot } },
          });
          const otherWorkflow = await renderChat({
            route,
            docked: true,
            workflowPermanentId: "wpid_other",
          });
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          const saved = { current: saveData.workflow };
          finishOnServer(saved);
          await act(async () => {
            call.onMessage(proposalResponse("Buffered old response."));
          });
          expect(screen.queryByText("Buffered old response.")).toBeNull();
          otherWorkflow.unmount();

          const returnedWorkflow = await queryClient.fetchQuery({
            queryKey,
            queryFn: async () => saved.current,
          });
          expect(returnedWorkflow.workflow_id).toBe("wf_committed");
          let resolveRead!: (response: { data: WorkflowApiResponse }) => void;
          const read = new Promise<{ data: WorkflowApiResponse }>((resolve) => {
            resolveRead = resolve;
          });
          historyGet.mockImplementation((path: string) =>
            path === "/workflows/wpid_1"
              ? read
              : Promise.resolve(historyResponse),
          );
          vi.useFakeTimers();
          await renderChat({ route, docked: true });
          const owner =
            useWorkflowYamlEditorStore.getState().editorOwner ??
            createYamlCommitOwner("wpid_1");
          registerEditorOwner(owner);
          expect(beginSaveTransaction(owner)).toBe(false);
          expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
          expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
          await act(async () => resolveRead({ data: saved.current }));
          expect(screen.getByRole("alert")).toBeTruthy();
          expect(beginSaveTransaction(owner)).toBe(false);
          await advance(2_000);
          await discardRecoveryDraft();
          expect(screen.queryByRole("alert")).toBeNull();
          expect(beginSaveTransaction(owner)).toBe(true);
          const request = buildWorkflowSaveRequest({
            ...saveData,
            blocks: getWorkflowBlocks(editorNodes, []),
          });
          expect(request.workflow_definition.blocks).toEqual(
            saved.current.workflow_definition.blocks,
          );
          expect(
            editorNodes.find((node) => node.id === "loop"),
          ).toBeUndefined();
          finishSaveTransaction(owner);
        },
      );

      it.each(["updated", "unchanged", "interrupted"] as const)(
        "resumes recovery after workflow navigation without losing local edits (%s canonical outcome)",
        async (outcome) => {
          changesState.hasChanges = true;
          saveData.workflow.workflow_definition = {
            blocks: [],
            parameters: [],
          };
          const { view, apply, saved } = await severStream();
          view.unmount();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          historyGet.mockClear();
          await advance(3_000);
          expect(historyGet).not.toHaveBeenCalled();

          vi.useRealTimers();
          const otherWorkflow = await renderChat({
            route,
            docked: true,
            workflowPermanentId: "wpid_other",
          });
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          expect(historyGet).not.toHaveBeenCalledWith("/workflows/wpid_1");
          otherWorkflow.unmount();
          historyGet.mockClear();
          const revisitedApply = vi.fn();
          await renderChat({
            route,
            docked: true,
            onWorkflowUpdate: revisitedApply,
          });
          if (vi.isFakeTimers()) await advance(2_000);
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).not.toBeNull();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).not.toBeNull();
          expect(revisitedApply).not.toHaveBeenCalled();
          act(editLoop);
          expect(
            editorNodes.find((node) => node.id === "loop")?.data,
          ).toMatchObject({
            loopValue: "unsaved_items",
          });

          const baseline = saved.current;
          finishOnServer(saved);
          if (outcome !== "updated") saved.current = baseline;
          if (outcome === "interrupted") {
            historyResponse.data.chat_history = [
              {
                sender: "ai",
                content: "Turn interrupted.",
                created_at: "2026-09-11T00:00:00Z",
                turn_outcome: {
                  copilot_turn_id: "turn-1",
                  terminal_reason: "interrupted",
                },
              },
            ];
          }
          if (outcome === "interrupted") {
            vi.useFakeTimers();
            await act(async () =>
              fireEvent.click(screen.getByRole("button", { name: "Retry" })),
            );
            await advance(120_000);
            expect(revisitedApply).not.toHaveBeenCalled();
            expect(
              useWorkflowYamlEditorStore.getState().copilotAcceptance,
            ).not.toBeNull();
            finishTurnHistory();
            await act(async () =>
              fireEvent.click(screen.getByRole("button", { name: "Retry" })),
            );
            expect(revisitedApply).not.toHaveBeenCalled();
            expect(
              useWorkflowYamlEditorStore.getState().copilotAcceptance,
            ).not.toBeNull();
            await advance(2_000);
          }
          if (outcome !== "interrupted") {
            vi.useFakeTimers();
            await act(async () => {
              fireEvent.click(screen.getByRole("button", { name: "Retry" }));
              await vi.advanceTimersByTimeAsync(2_000);
            });
          }
          expect(revisitedApply).not.toHaveBeenCalled();
          const generation =
            useWorkflowHasChangesStore.getState().saveGeneration;
          if (outcome === "updated") {
            expect(
              screen.getByRole("button", { name: "Keep my edits" }),
            ).toBeTruthy();
            await act(async () => {
              fireEvent.click(
                screen.getByRole("button", {
                  name: "Apply and discard my edits",
                }),
              );
              await vi.advanceTimersByTimeAsync(2_000);
            });
          } else {
            expect(
              screen.queryByRole("button", { name: "Keep my edits" }),
            ).toBeNull();
          }
          if (outcome === "updated") {
            expect(revisitedApply).toHaveBeenCalledExactlyOnceWith(
              saved.current,
              expect.objectContaining({ persisted: true, applied: true }),
            );
            expect(
              editorNodes.find((node) => node.id === "loop"),
            ).toBeUndefined();
          } else {
            expect(revisitedApply).not.toHaveBeenCalled();
            expect(
              editorNodes.find((node) => node.id === "loop")?.data,
            ).toMatchObject({ loopValue: "unsaved_items" });
            expect(useWorkflowHasChangesStore.getState().saveGeneration).toBe(
              generation,
            );
            expect(toast).not.toHaveBeenCalledWith(
              expect.objectContaining({
                description: expect.stringContaining("saved on the server"),
              }),
            );
          }
          expect(apply).not.toHaveBeenCalled();
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
        },
      );

      it.each(["late commit", "budget", "save in progress"] as const)(
        "retains the pre-turn recovery reservation and deadline across unmount: %s",
        async (outcome) => {
          changesState.hasChanges = true;
          const { view, saved } = await severStream(false);
          const originalReservation =
            useWorkflowYamlEditorStore.getState().copilotAcceptance;
          view.unmount();
          const persisted = canonicalRecoveriesByWorkflow.get("wpid_1")!;
          expect(persisted.poll?.turnId).toBeNull();
          expect(persisted.poll?.requestId).toBeTruthy();
          const poll = { ...persisted.poll! };
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          const owner =
            useWorkflowYamlEditorStore.getState().editorOwner ??
            createYamlCommitOwner("wpid_1");
          if (outcome === "save in progress") {
            registerEditorOwner(owner);
            expect(beginSaveTransaction(owner)).toBe(true);
          }
          const apply = vi.fn();
          const revisited = await renderChat({
            route,
            docked: true,
            onWorkflowUpdate: apply,
          });
          if (outcome === "save in progress") {
            expect(useWorkflowYamlEditorStore.getState().lockKind).toBe("save");
            await act(async () => finishSaveTransaction(owner));
          }
          const resumedReservation =
            useWorkflowYamlEditorStore.getState().copilotAcceptance;
          expect(resumedReservation).not.toBeNull();
          expect(resumedReservation).not.toBe(originalReservation);
          expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
          act(editLoop);
          expect(
            editorNodes.find((node) => node.id === "loop")?.data,
          ).toMatchObject({
            loopValue: "unsaved_items",
          });
          revisited.unmount();
          expect(canonicalRecoveriesByWorkflow.get("wpid_1")?.poll).toEqual(
            poll,
          );
          await renderChat({ route, docked: true, onWorkflowUpdate: apply });
          if (outcome === "budget") {
            await advance(Math.max(0, poll.deadline - Date.now() - 1));
            expect(
              useWorkflowYamlEditorStore.getState().copilotAcceptance,
            ).not.toBeNull();
            await advance(1);
            expect(apply).not.toHaveBeenCalled();
            expect(
              useWorkflowYamlEditorStore.getState().copilotAcceptance,
            ).not.toBeNull();
            finishOnServer(saved);
            await act(async () =>
              fireEvent.click(screen.getByRole("button", { name: "Retry" })),
            );
            await advance(0);
            await discardRecoveryDraft();
          } else {
            finishOnServer(saved);
            await advance(2_000);
            await discardRecoveryDraft();
            expect(apply).toHaveBeenCalledExactlyOnceWith(
              saved.current,
              expect.objectContaining({ persisted: true, applied: true }),
            );
          }
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull();
          expect(screen.getByText("Saved the workflow.")).toBeTruthy();
        },
      );

      it("keeps resumed recovery locked when a finished turn's canonical read fails", async () => {
        changesState.hasChanges = true;
        const { view, saved } = await severStream();
        view.unmount();
        finishOnServer(saved);
        let readFails = true;
        historyGet.mockImplementation((path: string) =>
          path === "/workflows/wpid_1"
            ? readFails
              ? Promise.reject(new Error("canonical unavailable"))
              : Promise.resolve({ data: saved.current })
            : Promise.resolve(historyResponse),
        );
        const apply = vi.fn();
        await renderChat({ route, docked: true, onWorkflowUpdate: apply });
        await advance(2_000);
        expect(apply).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        readFails = false;
        await advance(3_000);
        await discardRecoveryDraft();
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          saved.current,
          expect.objectContaining({ persisted: true, applied: true }),
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
      });

      it("retains a timed-out resumed recovery until Retry resolves canonical state", async () => {
        changesState.hasChanges = true;
        const view = await renderChat({ route, docked: true });
        await submit("edit the workflow");
        const call = streamCalls[0]!;
        historyGet.mockImplementation((path: string) =>
          path === "/workflows/wpid_1"
            ? Promise.reject(new Error("canonical unavailable"))
            : Promise.resolve(historyResponse),
        );
        vi.useFakeTimers();
        await act(async () => call.reject(new Error("connection dropped")));
        view.unmount();
        const deadline =
          canonicalRecoveriesByWorkflow.get("wpid_1")!.poll!.deadline;
        const apply = vi.fn();
        await renderChat({ route, docked: true, onWorkflowUpdate: apply });
        const reservation =
          useWorkflowYamlEditorStore.getState().copilotAcceptance;
        expect(reservation).not.toBeNull();
        await advance(Math.max(0, deadline - Date.now()));
        expect(screen.getByRole("alert").textContent).toContain(
          "Could not confirm whether Copilot saved changes",
        );
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
          reservation,
        );
        expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
        historyGet.mockClear();
        await advance(30_000);
        expect(historyGet).not.toHaveBeenCalled();
        const canonical = {
          ...saveData.workflow,
          workflow_id: "wf_saved",
          version: 2,
        };
        historyGet.mockImplementation((path: string) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: canonical }
              : historyResponse,
          ),
        );
        finishTurnHistory();
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Retry" })),
        );
        await advance(0);
        await discardRecoveryDraft();
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
        expect(screen.queryByRole("alert")).toBeNull();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
      });

      it.each([false, true])(
        "retries canonical before turn_start while refusing edits and unrelated history (edit attempted=%s)",
        async (edited) => {
          changesState.hasChanges = true;
          const { apply, saved } = await severStream(false);
          historyGet.mockClear();
          if (edited) {
            act(editLoop);
            expect(
              editorNodes.find((node) => node.id === "loop")?.data,
            ).toMatchObject({
              loopValue: "unsaved_items",
            });
          }
          await advance(2_000);
          expect(historyGet).toHaveBeenCalledWith(
            "/workflow/copilot/chat-history",
            expect.objectContaining({
              params: expect.objectContaining({
                request_cancel_token: streamCalls[0]!.body.cancel_token,
              }),
            }),
          );
          expect(apply).not.toHaveBeenCalled();
          finishOnServer(saved);
          await advance(3_000);
          expect(apply).toHaveBeenCalledExactlyOnceWith(
            saved.current,
            expect.objectContaining({
              persisted: true,
              applied: true,
            }),
          );
          expect(screen.getByText("Saved the workflow.")).toBeTruthy();
          expect(
            screen.queryByText(
              "The connection dropped, so Copilot is checking whether this turn finished.",
            ),
          ).toBeNull();
          historyGet.mockClear();
          await advance(30_000);
          expect(historyGet).not.toHaveBeenCalled();
        },
      );

      it("bounds canonical retries without turn_start", async () => {
        changesState.hasChanges = true;
        const { apply } = await severStream(false);
        historyGet.mockClear();
        await advance(2_000);
        expect(historyGet).toHaveBeenCalledWith(
          "/workflow/copilot/chat-history",
          expect.objectContaining({
            params: expect.objectContaining({
              request_cancel_token: streamCalls[0]!.body.cancel_token,
            }),
          }),
        );
        await advance(1_500_000);
        const reads = historyGet.mock.calls.length;
        expect(reads).toBeGreaterThan(1);
        expect(reads).toBeLessThan(60);
        await advance(1_500_000);
        expect(historyGet).toHaveBeenCalledTimes(reads);
        expect(apply).not.toHaveBeenCalled();
      });

      it("discards a canonical retry response after unmount before turn_start", async () => {
        changesState.hasChanges = true;
        const { view, apply, saved } = await severStream(false);
        finishOnServer(saved);
        let resolveRead!: (value: unknown) => void;
        historyGet.mockImplementation(
          () =>
            new Promise((resolve) => {
              resolveRead = resolve;
            }),
        );
        await advance(2_000);
        expect(resolveRead).toBeTypeOf("function");
        view.unmount();
        await act(async () => resolveRead({ data: saved.current }));
        expect(apply).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
        historyGet.mockClear();
        await advance(1_500_000);
        expect(historyGet).not.toHaveBeenCalled();
      });
    },
  );

  it("retains request-id recovery through an uncorrelated canonical advance before turn_start", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const retainedNodes = structuredClone(editorNodes);
    await submit("edit the workflow");
    const saved = {
      current: {
        ...saveData.workflow,
        workflow_id: "wf_another_tab",
        version: 2,
        workflow_definition: { blocks: [], parameters: [] },
      },
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saved.current }
          : historyResponse,
      ),
    );
    vi.useFakeTimers();
    await act(async () =>
      streamCalls[0]!.reject(new Error("connection dropped")),
    );
    expect(historyGet).toHaveBeenCalledWith(
      "/workflows/wpid_1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Another tab finished",
        turn_outcome: {
          copilot_turn_id: "another-turn",
          terminal_reason: null,
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    expect(apply).not.toHaveBeenCalled();
    expect(editorNodes).toEqual(retainedNodes);
    saved.current = { ...saved.current, workflow_id: "wf_copilot", version: 3 };
    finishTurnHistory();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(apply).toHaveBeenCalledExactlyOnceWith(
      saved.current,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it.each(["stream drop", "HTTP 500"])(
    "settles request-id recovery after %s before turn_start without a pause or question",
    async (failure) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const retainedNodes = structuredClone(editorNodes);
      const retainedParameters = structuredClone(
        useWorkflowParametersStore.getState().parameters,
      );
      const retainedTitle = useWorkflowTitleStore.getState().title;
      const retainedDescription = useWorkflowTitleStore.getState().description;
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      await submit("edit the workflow");
      vi.useFakeTimers();
      await act(async () =>
        streamCalls[0]!.reject(
          failure === "HTTP 500"
            ? Object.assign(new Error("Chat POST failed"), { status: 500 })
            : new Error("connection dropped"),
        ),
      );
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      historyGet.mockClear();
      finishTurnHistory();
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(historyGet).toHaveBeenCalledWith(
        "/workflow/copilot/chat-history",
        expect.objectContaining({
          params: expect.objectContaining({
            request_cancel_token: streamCalls[0]!.body.cancel_token,
          }),
        }),
      );
      expect(screen.getByText("Turn finished")).toBeTruthy();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(apply).not.toHaveBeenCalled();
      expect(editorNodes).toEqual(retainedNodes);
      expect(useWorkflowParametersStore.getState().parameters).toEqual(
        retainedParameters,
      );
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: retainedTitle,
        description: retainedDescription,
      });
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(beginSaveTransaction(owner)).toBe(true);
      finishSaveTransaction(owner);
    },
  );

  it.each(["network error", "SSE stream ended without terminal event"])(
    "restores the submitted draft when %s finds unchanged canonical state",
    async (failure) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: saveData.workflow }
            : historyResponse,
        ),
      );
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
      });
      apply.mockClear();
      vi.useFakeTimers();
      await act(async () => streamCalls[0]!.reject(new Error(failure)));
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(setEditorNodes).not.toHaveBeenCalled();
      finishTurnHistory();
      (
        historyResponse.data.chat_history[0] as {
          turn_outcome: { terminal_reason: string };
        }
      ).turn_outcome.terminal_reason = "error";
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(setEditorNodes).toHaveBeenCalledOnce();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(
        editorNodes.find((node) => node.id === "loop")?.data,
      ).toMatchObject({
        loopValue: "unsaved_items",
        loopVariableReference: "{{ item }}",
      });
    },
  );

  it.each(["Retry", "Reject", "timeout"])(
    "settles recovery after %s interrupts a canonical read",
    async (action) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: saveData.workflow }
            : historyResponse,
        ),
      );
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: "turn-1",
          error: "Finalizer still running",
        });
        streamCalls[0]!.resolve();
      });
      apply.mockClear();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Final commit saved",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: null,
          },
        },
      ];
      let readSignal: AbortSignal | undefined;
      let resolveRead!: (value: unknown) => void;
      historyGet.mockImplementation(
        (path: string, options?: { signal?: AbortSignal }) => {
          if (path !== "/workflows/wpid_1")
            return Promise.resolve(historyResponse);
          readSignal = options?.signal;
          return new Promise((resolve) => {
            resolveRead = resolve;
          });
        },
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(readSignal?.aborted).toBe(false);
      expect(historyGet).toHaveBeenLastCalledWith(
        "/workflows/wpid_1",
        expect.objectContaining({ timeout: 5_000, signal: readSignal }),
      );
      expect(apply).not.toHaveBeenCalled();
      if (action === "timeout") {
        await act(async () => vi.advanceTimersByTimeAsync(5_000));
        expect(readSignal?.aborted).toBe(true);
      }
      const canonical = {
        ...saveData.workflow,
        workflow_id: "wf_late_commit",
        version: 2,
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      historyGet.mockClear();

      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", {
            name: action === "Reject" ? "Reject" : "Retry",
          }),
        );
      });
      expect(readSignal?.aborted).toBe(true);
      await act(async () => vi.advanceTimersByTimeAsync(0));
      if (action === "Reject")
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          expect.anything(),
          { timeout: 5_000 },
        );
      expect(setEditorNodes).not.toHaveBeenCalled();
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.anything(),
      );
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true, applied: true }),
      );
      await act(async () => resolveRead({ data: saveData.workflow }));
      expect(apply).toHaveBeenCalledTimes(1);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it.each(["response", "failure", "canonical failure"])(
    "retries a settled recovery after a history %s",
    async (outcome) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      await submit("edit the workflow");
      let canonical = saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: "turn-1",
          error: "Finalizer still running",
        });
        streamCalls[0]!.resolve();
      });
      apply.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(50_000));
      if (outcome === "failure") {
        historyGet.mockRejectedValueOnce(new Error("offline"));
        await act(async () => vi.advanceTimersByTimeAsync(30_000));
      }
      expect(apply).not.toHaveBeenCalled();
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Final commit saved",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: null,
          },
        },
      ];
      if (outcome === "canonical failure") {
        historyGet.mockImplementation((path: string) =>
          path === "/workflows/wpid_1"
            ? Promise.reject(new Error("Canonical unavailable"))
            : Promise.resolve(historyResponse),
        );
        await act(async () => vi.advanceTimersByTimeAsync(30_000));
        expect(apply).not.toHaveBeenCalled();
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      historyGet.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(historyGet).not.toHaveBeenCalled();
      canonical = {
        ...saveData.workflow,
        workflow_id: "wf_late_commit",
        version: 2,
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(2_000);
      });
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.anything(),
      );
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true, applied: true }),
      );
    },
  );

  it.each(["late commit", "unchanged"])(
    "keeps pre-turn recovery reserved through %s canonical reads",
    async (outcome) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      let canonical = saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await submit("edit the workflow");
      vi.useFakeTimers();
      await act(async () => streamCalls[0]!.reject(new Error("network error")));
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
      if (outcome === "late commit") {
        canonical = {
          ...saveData.workflow,
          workflow_id: "wf_committed",
          version: 2,
        };
        finishTurnHistory();
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      } else {
        await act(async () => vi.advanceTimersByTimeAsync(1_499_999));
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        await act(async () => vi.advanceTimersByTimeAsync(1));
        expect(apply).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          {
            cancel_token: streamCalls[0]!.body.cancel_token,
            workflow_copilot_chat_id: "chat-1",
            source: "stop_button",
          },
          { timeout: 5_000 },
        );
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        return;
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it("keeps Turn off behind a legacy proposal's Accept request", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      // A v1 proposal carries no metadata, so a failed apply falls back to the client-side apply,
      // and that path writes the row itself instead of letting the apply route do it.
      streamCalls[0]!.onMessage(legacyProposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishRowWrite: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return new Promise((resolve) => (finishRowWrite = resolve));
      }
      return Promise.resolve({});
    });
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });

    // That write ends with auto_accept=true, so a Turn off sent first is silently overwritten.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(cancelPost.mock.calls.map(([path]) => path)).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );

    await act(async () => {
      finishRowWrite({ data: proposedWorkflowPayload() });
    });
    await waitFor(() =>
      expect(cancelPost.mock.calls.map(([path]) => path)).toContain(
        "/workflow/copilot/disable-auto-accept",
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
  });

  it("keeps a typed proposal reserved when atomic Accept has an uncertain outcome", async () => {
    changesState.hasChanges = true;
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
  });

  it("parks manual Accept on unmount and ignores its late response in another workflow", async () => {
    const apply = vi.fn((workflow: WorkflowApiResponse) => {
      useWorkflowTitleStore.getState().setTitle(workflow.title);
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromWorkflow(workflow.description);
      useWorkflowParametersStore.getState().setParameters([]);
    });
    const view = await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveAccept!: (response: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAccept = resolve;
        }),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.anything(),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();

    vi.useFakeTimers();
    view.unmount();
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts.wpid_1,
    ).toBeDefined();
    registerEditorOwner(createYamlCommitOwner("wpid_2"));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    const parameter = {
      key: "next_input",
      parameterType: "context" as const,
      sourceParameterKey: "source",
    };
    useWorkflowTitleStore.getState().setTitle("Next workflow");
    useWorkflowTitleStore
      .getState()
      .setDescriptionFromWorkflow("Next description");
    useWorkflowParametersStore.getState().setParameters([parameter]);
    expect(useWorkflowTitleStore.getState().title).toBe("Next workflow");
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      parameter,
    ]);
    const titleState = useWorkflowTitleStore.getState();
    const parametersState = useWorkflowParametersStore.getState();

    useWorkflowYamlEditorStore.getState().open("next draft");
    const yamlState = useWorkflowYamlEditorStore.getState();
    const readCount = historyGet.mock.calls.length;
    await act(async () =>
      resolveAccept({
        data: proposedWorkflowPayload({ description: "Late description" }),
      }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(historyGet).toHaveBeenCalledTimes(readCount);
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      draft: yamlState.draft,
      entrySnapshot: yamlState.entrySnapshot,
    });
    expect(apply).not.toHaveBeenCalled();
    expect(useWorkflowTitleStore.getState()).toBe(titleState);
    expect(useWorkflowParametersStore.getState()).toBe(parametersState);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().lockKind).toBeNull();
  });

  it("refreshes YAML with the accepted title when no title input was authored", async () => {
    changesState.hasChanges = true;
    await renderChat({
      onWorkflowUpdate: (workflow) =>
        useWorkflowTitleStore.getState().syncTitleFromWorkflow(workflow.title),
    });
    useWorkflowTitleStore.getState().setTitle("Current custom title");
    act(() =>
      useWorkflowYamlEditorStore.getState().open("title: Current custom title"),
    );
    await submit("edit the workflow");
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          updated_workflow: {
            ...saveData.workflow,
            title: "New Workflow",
            workflow_definition: { version: 2, parameters: [], blocks: [] },
          },
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(useWorkflowTitleStore.getState().title).toBe("New Workflow");
    expect(parse(useWorkflowYamlEditorStore.getState().draft).title).toBe(
      "New Workflow",
    );
  });

  it.each(["terminal", "manual", "fallback", "canonical"])(
    "refreshes untouched YAML after a %s apply",
    async (mode) => {
      changesState.hasChanges = true;
      const appliedWorkflow = {
        ...saveData.workflow,
        workflow_id: "wf_accepted",
        version: 2,
        title: "Accepted title",
        description: "Accepted description",
        workflow_definition: { version: 2, parameters: [], blocks: [] },
        enable_self_healing: true,
        mask_secrets: true,
      } as WorkflowApiResponse;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      act(() => useWorkflowYamlEditorStore.getState().open("title: Original"));
      await submit("edit the workflow");
      if (mode === "canonical") {
        historyGet.mockImplementation((path: string) =>
          Promise.resolve(
            path === "/workflows/wpid_1"
              ? { data: appliedWorkflow }
              : historyResponse,
          ),
        );
        vi.useFakeTimers();
        await act(async () =>
          streamCalls[0]!.reject(new Error("network error")),
        );
        expect(apply).not.toHaveBeenCalled();
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(
          "title: Original",
        );
        historyResponse.data.request_turn_id = "turn-1";
        historyResponse.data.chat_history = [
          {
            sender: "ai",
            content: "Changes saved.",
            created_at: new Date().toISOString(),
            turn_outcome: {
              copilot_turn_id: "turn-1",
              terminal_reason: null,
            },
          },
        ];
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(historyGet).toHaveBeenCalledWith(
          "/workflow/copilot/chat-history",
          expect.objectContaining({
            params: expect.objectContaining({
              request_cancel_token: streamCalls[0]!.body.cancel_token,
            }),
          }),
        );
        expect(apply).toHaveBeenCalled();
      } else {
        await act(async () => {
          streamCalls[0]!.onMessage(
            proposalResponse("Ready.", {
              updated_workflow: appliedWorkflow,
              ...(mode === "terminal"
                ? {
                    proposal_disposition: "auto_applicable",
                    workflow_applied: true,
                  }
                : {}),
            }),
          );
          streamCalls[0]!.resolve();
        });
        if (mode !== "terminal") {
          if (mode === "fallback")
            cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
          else cancelPost.mockResolvedValueOnce({ data: appliedWorkflow });
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "Accept" })),
          );
        }
      }
      const state = useWorkflowYamlEditorStore.getState();
      expect(state.active).toBe(true);
      if (mode === "fallback") {
        expect(state.draft).toBe("title: Original");
        return;
      }
      if (mode !== "fallback") expect(state.error).toBeNull();
      expect(parse(state.draft)).toMatchObject({
        title: "Accepted title",
        description: "Accepted description",
        mask_secrets: true,
        cdp_connect_headers: { Authorization: "********" },
        totp_identifier: saveData.settings.totpIdentifier,
        workflow_definition: { version: 2, blocks: [] },
      });
      expect(state.entrySnapshot).toBe(state.draft);
    },
  );

  it("waits for the errored turn's final history row before applying a late commit", async () => {
    changesState.hasChanges = true;
    historyResponse.data.auto_accept = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await submit("edit the workflow");
    let canonical = saveData.workflow;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
      });
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
    });
    apply.mockClear();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "error",
        turn_id: "turn-1",
        error: "Finalizer still running",
      });
      streamCalls[0]!.resolve();
    });
    expect(apply).not.toHaveBeenCalled();
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    expect(
      screen.getByRole("button", { name: "Retry" }).getAttribute("disabled"),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Reject" }).getAttribute("disabled"),
    ).toBeNull();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Other turn",
        turn_outcome: {
          copilot_turn_id: "turn-other",
          terminal_reason: null,
        },
      },
    ];
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apply).not.toHaveBeenCalled();
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.objectContaining({ source: "stop_button" }),
      expect.anything(),
    );
    canonical = {
      ...saveData.workflow,
      workflow_id: "wf_late_commit",
      version: 2,
      workflow_definition: { parameters: [], blocks: [] },
    };
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Interrupted",
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "interrupted",
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(120_000));
    expect(apply).not.toHaveBeenCalled();
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Final commit saved",
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: null,
        },
      },
    ];
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    expect(apply).not.toHaveBeenCalled();
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    historyGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
      "/workflow/copilot/chat-history",
      "/workflows/wpid_1",
    ]);
    expect(apply).toHaveBeenCalledExactlyOnceWith(
      canonical,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("reconciles a refused persisted terminal after YAML unlock and retains the losing draft", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const canonical = {
      ...saveData.workflow,
      title: "Canonical Copilot change",
      version: 2,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await submit("edit the workflow");
    // Simulate a stale writer that bypassed beginYamlCommit's reservation check.
    act(() => {
      useWorkflowYamlEditorStore.getState().open("title: Original");
      useWorkflowYamlEditorStore.setState({
        draft: "title: My YAML draft",
        commitInProgress: true,
      });
    });
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Applied.", {
          updated_workflow: canonical,
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(apply).not.toHaveBeenCalled();
    expect(historyGet).not.toHaveBeenCalledWith(
      "/workflows/wpid_1",
      expect.anything(),
    );
    await act(async () => {
      useWorkflowYamlEditorStore.getState().close();
      useWorkflowYamlEditorStore.getState().setCommitInProgress(false);
    });
    await waitFor(() =>
      expect(apply).toHaveBeenCalledWith(
        canonical,
        expect.objectContaining({
          persisted: true,
          applied: true,
          settings: expect.objectContaining({
            cdpConnectHeaders: saveData.settings.cdpConnectHeaders,
            extraHttpHeaders: saveData.settings.extraHttpHeaders,
            totpIdentifier: saveData.settings.totpIdentifier,
            totpVerificationUrl: saveData.settings.totpVerificationUrl,
          }),
        }),
      ),
    );
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      active: true,
      draft: "title: My YAML draft",
      entrySnapshot: "title: Original",
      error:
        "The workflow changed while YAML was open. Reopen the YAML view to continue.",
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it.each(["server", "missing-chat", "failed-server"])(
    "restores headers on manual Accept through %s",
    async (mode) => {
      changesState.hasChanges = true;
      saveData.settings.cdpConnectHeaders = '{"Authorization":"accept-cdp"}';
      saveData.settings.extraHttpHeaders = '{"X-Token":"accept-extra"}';
      const snapshot = structuredClone(saveData.settings);
      let editorSettings = snapshot;
      await renderChat({
        onWorkflowUpdate: (workflow, options) => {
          editorSettings = options?.settings ?? apiWorkflowToSettings(workflow);
          saveData.settings = editorSettings;
          changesState.setHasChanges(!options?.persisted);
        },
      });
      await submit("edit the workflow");
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        streamCalls[0]!.onMessage(
          proposalResponse("Draft ready.", {
            workflow_copilot_chat_id: mode === "missing-chat" ? "" : "chat-1",
          }),
        );
        streamCalls[0]!.resolve();
      });
      if (mode === "missing-chat") {
        historyResponse.data.workflow_copilot_chat_id = null;
      } else if (mode === "failed-server") {
        cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
      } else {
        cancelPost.mockResolvedValueOnce({
          data: proposedWorkflowPayload({
            extra_http_headers: null,
            cdp_connect_headers: { Authorization: "***" },
          }),
        });
      }
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Accept" })),
      );
      expect(editorSettings).toMatchObject({
        cdpConnectHeaders: snapshot.cdpConnectHeaders,
        extraHttpHeaders: snapshot.extraHttpHeaders,
        totpIdentifier: snapshot.totpIdentifier,
        totpVerificationUrl: snapshot.totpVerificationUrl,
      });
      if (mode === "success")
        expect(changesState.setHasChanges).toHaveBeenLastCalledWith(true);
    },
  );

  it.each(["unresolved chat", "HTTP rejection"])(
    "keeps a failed local Accept reserved after %s until Retry applies it",
    async (mode) => {
      changesState.hasChanges = true;
      const proposal = proposedWorkflowPayload({
        workflow_definition: { parameters: [], blocks: [] },
      });
      historyResponse.data.proposed_workflow = proposal;
      if (mode === "unresolved chat")
        historyResponse.data.workflow_copilot_chat_id = null;
      const apply = vi.fn((): void => {
        throw new Error("Canvas unavailable");
      });
      await renderChat({ onWorkflowUpdate: apply });
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      if (mode === "HTTP rejection")
        cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
      vi.useFakeTimers();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Accept" })),
      );
      expect(apply).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
      expect(beginSaveTransaction(owner)).toBe(mode === "unresolved chat");
      finishSaveTransaction(owner);
    },
  );

  it("A42 enables later questions when the answer POST settles after recovery", async () => {
    changesState.hasChanges = true;
    changesState.hasChanges = false;
    let loadHistory!: (value: unknown) => void;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          loadHistory = resolve;
        }),
    );
    await renderChat();
    vi.useFakeTimers();
    await act(async () => loadHistory({ data: pausedHistory() }));
    let finishAnswer!: (value: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAnswer = resolve;
        }),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Skip" })),
    );
    const canonical = {
      ...saveData.workflow,
      ...proposedWorkflowPayload({ workflow_id: "wf_resumed" }),
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve({
        data: path === "/workflows/wpid_1" ? canonical : completedHistory(),
      }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await act(async () =>
      finishAnswer({ data: { ...recoveredQuestion(), status: "resolved" } }),
    );
    vi.useRealTimers();
    await submit("continue");
    await act(async () =>
      streamCalls[0]!.onMessage({
        type: "question_required",
        turn_id: "turn-next",
        workflow_copilot_chat_id: "chat-1",
        cancel_token: "next-token",
        interactions: [
          {
            ...recoveredQuestion(),
            interaction_id: "question-next",
            turn_id: "turn-next",
          },
        ],
      }),
    );
    expect(
      (screen.getByRole("button", { name: "Skip" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it.each(["question", "credential"])(
    "A42 recovery Reject sends the original %s cancellation token and chat",
    async (kind) => {
      changesState.hasChanges = false;
      let cancellationToken = "original-cancel-token";
      if (kind === "credential") {
        const first = await renderChat();
        await submit("Sign in");
        cancellationToken = streamCalls[0]!.body.cancel_token;
        first.unmount();
        historyGet.mockImplementation((_path, config) =>
          Promise.resolve({
            data: {
              ...pausedHistory(kind),
              request_turn_id:
                config?.params?.request_cancel_token === cancellationToken
                  ? "turn-recovered"
                  : null,
            },
          }),
        );
      } else historyGet.mockResolvedValue({ data: pausedHistory(kind) });
      await renderChat();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Reject" })),
      );
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        {
          cancel_token: cancellationToken,
          workflow_copilot_chat_id: "chat-1",
          source: "stop_button",
        },
        expect.anything(),
      );
    },
  );

  it.each(
    [
      "question",
      "credential",
      "external question",
      "external credential",
    ].flatMap((path) =>
      ["Keep my edits", "Apply and discard my edits"].map((choice) => [
        path,
        choice,
      ]),
    ),
  )(
    "A42 protects local inputs before claiming delayed %s history: %s",
    async (path, choice) => {
      changesState.hasChanges = true;
      changesState.hasChanges = false;
      let historyLoaded!: (value: unknown) => void;
      historyGet.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            historyLoaded = resolve;
          }),
      );
      const apply = vi.fn(() =>
        useWorkflowParametersStore.getState().setParameters([]),
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      await renderChat({ onWorkflowUpdate: apply });
      const input = {
        key: "local_input",
        parameterType: "context" as const,
        sourceParameterKey: "source",
      };
      useWorkflowParametersStore.getState().setParameters([input]);
      useWorkflowTitleStore.getState().setTitle("Local title");
      useWorkflowYamlEditorStore.getState().open("title: Baseline");
      useWorkflowYamlEditorStore.getState().setDraft("title: Local YAML");
      changesState.hasChanges = true;
      vi.useFakeTimers();
      await act(async () =>
        historyLoaded({
          data: pausedHistory(
            path?.includes("credential") ? "credential" : "question",
          ),
        }),
      );
      if (!path?.startsWith("external")) {
        cancelPost.mockResolvedValueOnce({
          data: { ...recoveredQuestion(), status: "resolved" },
        });
        await act(async () =>
          fireEvent.click(
            screen.getByRole("button", {
              name: path === "credential" ? "Skip for now" : "Skip",
            }),
          ),
        );
      }
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({
          workflow_id: "wf_resumed",
          title: "New Workflow",
          workflow_definition: { parameters: [], blocks: [] },
        }),
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve({
          data: path === "/workflows/wpid_1" ? canonical : completedHistory(),
        }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(apply).not.toHaveBeenCalled();
      expect(useWorkflowParametersStore.getState().parameters).toEqual([input]);
      expect(useWorkflowTitleStore.getState().title).toBe("Local title");
      expect(useWorkflowYamlEditorStore.getState().draft).toBe(
        "title: Local YAML",
      );
      expect(beginSaveTransaction(owner)).toBe(false);
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: choice }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      if (choice === "Keep my edits") {
        expect(apply).not.toHaveBeenCalled();
        expect(useWorkflowParametersStore.getState().parameters).toEqual([
          input,
        ]);
        expect(useWorkflowTitleStore.getState().title).toBe("Local title");
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(
          "title: Local YAML",
        );
      } else {
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({ persisted: true }),
        );
        expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
        expect(useWorkflowTitleStore.getState().title).toBe(canonical.title);
        const yaml = useWorkflowYamlEditorStore.getState();
        expect(parse(yaml.draft)).toMatchObject({ title: canonical.title });
        expect(yaml.entrySnapshot).toBe(yaml.draft);
        expect(yaml.stale).toBe(false);
      }
    },
  );

  it.each(["canonical", "proposal"])(
    "A42 retains uncertain Accept only while the %s read fails, then Retry settles",
    async (failedRead) => {
      changesState.hasChanges = true;
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({ workflow_id: "wf_accepted", version: 2 }),
      };
      historyResponse.data.proposed_workflow = canonical;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole(
        "button",
        { name: "Accept" },
        { timeout: 10_000 },
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      cancelPost.mockRejectedValueOnce(new Error("Accept response lost"));
      historyGet.mockImplementation((path: string) => {
        if ((path === "/workflows/wpid_1") === (failedRead === "canonical"))
          return Promise.reject(new Error("read unavailable"));
        return Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        );
      });
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
      expect(apply).not.toHaveBeenCalled();
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
      await act(async () => {
        fireEvent.click(
          within(
            screen.getByText(/Could not confirm whether Copilot saved changes/)
              .parentElement!,
          ).getByRole("button", { name: "Retry" }),
        );
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(cancelPost).toHaveBeenCalledTimes(1);
      expect(beginSaveTransaction(owner)).toBe(false);
      historyResponse.data.proposed_workflow = null;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true }),
      );
      expect(cancelPost).toHaveBeenCalledTimes(1);
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each([
    "interrupted commit",
    "legacy commit",
    "legacy unchanged",
    "500 unchanged",
    "cleared unchanged",
    "pending newer commit",
  ])(
    "A42 settles uncertain Accept from proposal evidence: %s",
    async (outcome) => {
      changesState.hasChanges = true;
      const baseline = {
        ...saveData.workflow,
        version: 1,
        modified_at: "2026-09-01T00:00:00Z",
      };
      saveData.workflow = baseline;
      const canonical = {
        ...baseline,
        ...proposedWorkflowPayload({ workflow_id: "wf_accepted" }),
        version: 2,
        modified_at: "2026-09-02T00:00:00Z",
      };
      historyResponse.data.proposed_workflow = canonical;
      if (!outcome.startsWith("legacy")) {
        historyResponse.data.proposed_workflow_metadata = {
          owner_turn_id: "turn-recovered",
          revision: 1,
          canonical_fingerprint: "baseline",
          disposition: "review_untested",
          workflow_run_id: null,
        };
        historyResponse.data.chat_history = pausedHistory().chat_history;
      }
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole(
        "button",
        { name: "Accept" },
        { timeout: 10_000 },
      );
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      cancelPost.mockRejectedValueOnce(
        Object.assign(new Error("accept failed"), {
          response: { status: 500 },
        }),
      );
      const committed =
        outcome.endsWith("commit") || outcome === "cleared unchanged";
      const saved = outcome === "cleared unchanged" ? baseline : canonical;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: committed ? saved : baseline }
            : historyResponse,
        ),
      );
      historyGet.mockClear();
      vi.useFakeTimers();
      await act(async () => fireEvent.click(accept));
      expect(apply).not.toHaveBeenCalled();
      expect(historyGet).not.toHaveBeenCalled();
      expect(beginSaveTransaction(owner)).toBe(false);
      if (committed && outcome !== "pending newer commit")
        historyResponse.data.proposed_workflow = null;
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      if (!committed || outcome === "pending newer commit") {
        expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
          "/workflow/copilot/chat-history",
        ]);
        expect(apply).not.toHaveBeenCalled();
        expect(beginSaveTransaction(owner)).toBe(false);
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        return;
      }
      expect(historyGet.mock.calls.map(([path]) => path)).toEqual([
        "/workflow/copilot/chat-history",
        "/workflows/wpid_1",
      ]);
      expect(historyGet.mock.calls[0]?.[1]?.params).toEqual({
        workflow_copilot_chat_id: "chat-1",
      });
      if (committed)
        expect(apply).toHaveBeenCalledWith(
          saved,
          expect.objectContaining({ persisted: true }),
        );
      else {
        expect(apply).not.toHaveBeenCalled();
        expect(toast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: "Accept failed",
            variant: "destructive",
          }),
        );
        expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each(["legacy", "metadata"])(
    "releases a %s proposal with local edits after Accept returns 400",
    async (kind) => {
      changesState.hasChanges = true;
      const proposal = proposedWorkflowPayload({
        workflow_definition: { parameters: [], blocks: [] },
      });
      historyResponse.data.proposed_workflow = proposal;
      if (kind === "metadata") {
        historyResponse.data.proposed_workflow_metadata = {
          owner_turn_id: "turn-proposed",
          revision: 1,
          canonical_fingerprint: "baseline",
          disposition: "review_untested",
          workflow_run_id: null,
        };
      }
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      historyGet.mockClear();
      cancelPost.mockRejectedValueOnce({ response: { status: 400 } });

      await act(async () => fireEvent.click(accept));

      expect(apply).not.toHaveBeenCalled();
      expect(cancelPost).toHaveBeenCalledOnce();
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
      expect(screen.getByText("Not saved")).toBeTruthy();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      expect(beginSaveTransaction(owner)).toBe(true);
    },
  );

  it.each(["success", "lost response"])(
    "A42 invalidates disposed Accept caches on %s and refetches on reopen",
    async (outcome) => {
      changesState.hasChanges = true;
      const canonical = {
        ...saveData.workflow,
        ...proposedWorkflowPayload({ workflow_id: "wf_accepted" }),
      };
      historyResponse.data.proposed_workflow = canonical;
      const keys = [
        ["workflow", "wpid_1"],
        ["workflows"],
        ["block-scripts", "wpid_1"],
      ];
      for (const key of keys) queryClient.setQueryData(key, saveData.workflow);
      const apply = vi.fn();
      const view = await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole(
        "button",
        { name: "Accept" },
        { timeout: 10_000 },
      );
      let settle!: (value: unknown) => void;
      let fail!: (error: Error) => void;
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((resolve, reject) => {
            settle = resolve;
            fail = reject;
          }),
      );
      await act(async () => fireEvent.click(accept));
      await waitFor(() => expect(cancelPost).toHaveBeenCalledOnce(), {
        timeout: 10_000,
      });
      view.unmount();
      await act(async () => {
        if (outcome === "success") settle({ data: canonical });
        else fail(new Error("response lost after commit"));
      });
      expect(apply).not.toHaveBeenCalled();
      for (const key of keys)
        expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
      const fetch = vi.fn().mockResolvedValue(canonical);
      expect(
        await queryClient.fetchQuery({ queryKey: keys[0]!, queryFn: fetch }),
      ).toEqual(canonical);
      expect(fetch).toHaveBeenCalledOnce();
      queryClient.clear();
    },
  );
});

describe("A46 Accept recovery", () => {
  function pendingProposal(
    disposition: "review_untested" | "accepting" = "review_untested",
  ) {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-pending",
      revision: 1,
      disposition,
      canonical_fingerprint: "baseline",
      workflow_run_id: null,
    };
  }

  it("A46 keeps an unchanged proposal reserved across timeout and Retry conflict", async () => {
    pendingProposal();
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    await screen.findByRole("button", { name: "Accept" });
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    cancelPost.mockImplementationOnce(() => new Promise(() => {}));
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    await act(async () => vi.advanceTimersByTimeAsync(32_000));
    expect(beginSaveTransaction(owner)).toBe(false);
    cancelPost.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: "Copilot proposal is already being accepted" },
      },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(
      cancelPost.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/apply-proposed-workflow",
      ),
    ).toHaveLength(2);
    expect(beginSaveTransaction(owner)).toBe(false);
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    const canonical = { ...saveData.workflow, version: 2 };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apply).toHaveBeenCalledWith(
      canonical,
      expect.objectContaining({ persisted: true }),
    );
    expect(beginSaveTransaction(owner)).toBe(true);
  });

  it("A46 reload reads the stored accepting chat rather than the latest chat", async () => {
    pendingProposal();
    await renderChat();
    const accept = await screen.findByRole("button", { name: "Accept" });
    cancelPost.mockRejectedValueOnce(new Error("lost response"));
    await act(async () => fireEvent.click(accept));
    expect(
      JSON.parse(sessionStorage.getItem("copilot-pending-accept:wpid_1")!),
    ).toMatchObject({
      chatId: "chat-1",
      acceptAttempt: { owner_turn_id: "turn-pending", revision: 1 },
    });
    cleanup();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    pendingProposal("accepting");
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    const original = structuredClone(historyResponse);
    historyGet.mockClear();
    historyGet.mockImplementation(
      (
        _path: string,
        config?: { params?: { workflow_copilot_chat_id?: string } },
      ) =>
        Promise.resolve(
          config?.params?.workflow_copilot_chat_id === "chat-1"
            ? original
            : {
                data: {
                  ...original.data,
                  workflow_copilot_chat_id: "chat-2",
                  proposed_workflow: null,
                  proposed_workflow_metadata: null,
                  proposed_claim_expires_in_seconds: null,
                },
              },
        ),
    );
    await renderChat();
    await act(async () => {});
    expect(historyGet.mock.calls[0]?.[1]?.params.workflow_copilot_chat_id).toBe(
      "chat-1",
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it.each([null, 0, -1])(
    "A46 restores an accepting row with liveness %s",
    async (remaining) => {
      pendingProposal("accepting");
      historyResponse.data.proposed_claim_expires_in_seconds = remaining;
      await renderChat();
      await act(async () => {});
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      expect(
        useWorkflowHasChangesStore.getState().saveBlockedReason,
      ).toBeTruthy();
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    },
  );

  it("A46 holds startup navigation and Save until a delayed claim is installed", async () => {
    pendingProposal("accepting");
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    let resolveHistory!: (value: unknown) => void;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    await renderChat({ docked: true });
    expect(
      useWorkflowHasChangesStore.getState().saveBlockedReason,
    ).toBeTruthy();
    expect(useCopilotHeaderStore.getState().controls?.newChatDisabled).toBe(
      true,
    );
    await act(async () =>
      useCopilotHeaderStore.getState().controls?.onNewChat(),
    );
    await act(async () =>
      useCopilotHeaderStore.getState().controls?.onSelectChat({
        workflow_copilot_chat_id: "chat-other",
      } as WorkflowCopilotChatSummary),
    );
    expect(historyGet).toHaveBeenCalledTimes(1);
    await act(async () => resolveHistory(historyResponse));
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("A46 retries failed startup without unlocking Save", async () => {
    historyGet.mockRejectedValue(new Error("history unavailable"));
    await renderChat();
    await act(async () => {});
    expect(
      useWorkflowHasChangesStore.getState().saveBlockedReason,
    ).toBeTruthy();
    const retry = screen.getByRole("button", { name: "Retry" });
    historyGet.mockResolvedValue(historyResponse);
    await act(async () => fireEvent.click(retry));
    expect(useWorkflowHasChangesStore.getState().saveBlockedReason).toBeNull();
  });

  it("A46 installs a startup claim after an already-running Save finishes", async () => {
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    expect(beginSaveTransaction(owner)).toBe(true);
    pendingProposal("accepting");
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    await renderChat();
    await act(async () => {});
    expect(
      useWorkflowHasChangesStore.getState().saveBlockedReason,
    ).toBeTruthy();
    await act(async () => finishSaveTransaction(owner));
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("A46 refuses Turn off while an Always accept outcome remains uncertain", async () => {
    pendingProposal();
    historyResponse.data.auto_accept = true;
    await renderChat();
    await screen.findByRole("button", { name: "Accept" });
    cancelPost.mockRejectedValueOnce(new Error("lost response"));
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Always accept" })),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: /Turn off/ })),
    );
    expect(
      cancelPost.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/disable-auto-accept",
      ),
    ).toHaveLength(0);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("A47 refuses Turn off during deferred startup and allows it only after the claim settles", async () => {
    const owner = createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    expect(beginSaveTransaction(owner)).toBe(true);
    pendingProposal("accepting");
    historyResponse.data.auto_accept = true;
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    await renderChat();
    await act(async () => {});
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
    ).toBeUndefined();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: /Turn off/ })),
    );
    expect(
      cancelPost.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/disable-auto-accept",
      ),
    ).toHaveLength(0);
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Auto-accept is still on",
        description: "Wait for the Copilot change to finish",
      }),
    );
    await act(async () => finishSaveTransaction(owner));
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
    ).toBeDefined();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: /Turn off/ })),
    );
    expect(
      cancelPost.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/disable-auto-accept",
      ),
    ).toHaveLength(0);
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    changesState.hasChanges = false;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    await waitFor(() =>
      expect(
        useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
      ).toBeUndefined(),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: /Turn off/ })),
    );
    expect(
      cancelPost.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/disable-auto-accept",
      ),
    ).toHaveLength(1);
  });

  it.each(["history", "stream"])(
    "A47 keeps an accepted description change clean without a local edit (%s)",
    async (source) => {
      if (source === "history") pendingProposal();
      saveData.workflow.description =
        source === "history"
          ? "Original description"
          : "Previously saved description";
      saveData.workflow.proxy_location = null;
      saveData.description = "Original description";
      saveData.settings = apiWorkflowToSettings(saveData.workflow);
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromWorkflow(saveData.description);
      const saved = {
        ...saveData.workflow,
        description: "Accepted description",
      };
      if (source === "history") historyResponse.data.proposed_workflow = saved;
      const apply = vi.fn((workflow: WorkflowApiResponse) => {
        useWorkflowTitleStore
          .getState()
          .setDescriptionFromWorkflow(workflow.description);
        useWorkflowHasChangesStore.getState().setHasChanges(false);
      });
      await renderChat({ onWorkflowUpdate: apply });
      if (source === "stream") {
        await submit("Update the description");
        await waitFor(() => expect(postStreaming).toHaveBeenCalledOnce());
        await act(async () => {
          streamCalls[0]!.onMessage(
            proposalResponse("Description ready", { updated_workflow: saved }),
          );
          streamCalls[0]!.resolve();
        });
      }
      const accept = await screen.findByRole("button", { name: "Accept" });
      cancelPost.mockResolvedValueOnce({ data: saved });
      await act(async () => fireEvent.click(accept));
      expect(apply).toHaveBeenCalled();
      expect(useWorkflowTitleStore.getState().description).toBe(
        "Accepted description",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    },
  );

  it("keeps accepted draft B after streamed draft C without inferring a user description edit", async () => {
    saveData.workflow.proxy_location = null;
    saveData.settings = apiWorkflowToSettings(saveData.workflow);
    saveData.description = "A";
    const accepted = { ...saveData.workflow, description: "B" };
    const apply = vi.fn((workflow: WorkflowApiResponse) => {
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromWorkflow(workflow.description);
      saveData.description = workflow.description;
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    });
    await renderChat({ onWorkflowUpdate: apply });
    await submit("Update description");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledOnce());
    await act(async () => {
      for (const description of ["B", "C"])
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: { ...accepted, description },
        });
      streamCalls[0]!.onMessage(
        proposalResponse("Draft B recovered", { updated_workflow: accepted }),
      );
      streamCalls[0]!.resolve();
    });
    expect(useWorkflowTitleStore.getState().description).toBe("C");
    cancelPost.mockResolvedValueOnce({ data: accepted });
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    expect(useWorkflowTitleStore.getState().description).toBe("B");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  it.each(["title", "description"] as const)(
    "keeps the user's %s edit after a later Copilot write and Accept",
    async (field) => {
      pendingProposal();
      const saved = {
        ...saveData.workflow,
        title: "Original",
        description: "Proposal description",
      };
      const apply = vi.fn((workflow: WorkflowApiResponse) => {
        useWorkflowTitleStore.getState().syncTitleFromWorkflow(workflow.title);
        useWorkflowTitleStore
          .getState()
          .setDescriptionFromWorkflow(workflow.description);
        useWorkflowHasChangesStore.getState().setHasChanges(false);
      });
      await renderChat({ onWorkflowUpdate: apply });
      const accept = await screen.findByRole("button", { name: "Accept" });
      const titles = useWorkflowTitleStore.getState();
      if (field === "title") titles.setTitle("My rename");
      else titles.setDescriptionFromUser("My description");
      titles.syncTitleFromWorkflow("Later draft title");
      titles.setDescriptionFromWorkflow("Later draft description");
      saveData.description = "Later draft description";
      cancelPost.mockResolvedValueOnce({ data: saved });
      await act(async () => fireEvent.click(accept));
      expect(useWorkflowTitleStore.getState()[field]).toBe(
        field === "title" ? "My rename" : "My description",
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    },
  );

  it("releases a definitive Accept rejection after its editor unmounts", async () => {
    pendingProposal();
    const view = await renderChat();
    const accept = await screen.findByRole("button", { name: "Accept" });
    let reject!: (error: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    await act(async () => fireEvent.click(accept));
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
    ).toBeDefined();
    view.unmount();
    await act(async () =>
      reject({
        response: {
          status: 409,
          data: { detail: "Workflow changed after this proposal" },
        },
      }),
    );
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
    ).toBeUndefined();
    expect(sessionStorage.getItem("copilot-pending-accept:wpid_1")).toBeNull();
  });

  it("A49 presents a definitive rejection to the remounted editor", async () => {
    pendingProposal();
    const view = await renderChat();
    const accept = await screen.findByRole("button", { name: "Accept" });
    let reject!: (error: unknown) => void;
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    await act(async () => fireEvent.click(accept));
    view.unmount();
    await renderChat();
    await screen.findByRole("button", { name: "Retry" });
    expect(
      screen.getByText(/Copilot is checking for saved changes/),
    ).toBeTruthy();
    await act(async () =>
      reject({
        response: {
          status: 409,
          data: { detail: "Workflow changed after this proposal" },
        },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull(),
    );
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
    expect(
      screen.queryByText(/Copilot is checking for saved changes/),
    ).toBeNull();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Accept failed",
        description: "Workflow changed after this proposal",
      }),
    );
    expect(
      useWorkflowYamlEditorStore.getState().pendingAccepts["wpid_1"],
    ).toBeUndefined();
    expect(sessionStorage.getItem("copilot-pending-accept:wpid_1")).toBeNull();
  });

  it.each([
    [409, "Workflow changed after this proposal", true],
    [409, "Copilot proposal changed; reload required", true],
    [409, "Copilot proposal metadata is invalid; reload required", true],
    [400, "No proposed workflow to apply", true],
    [400, "Proposed workflow has no copilot YAML to apply", true],
    [400, "Proposed copilot YAML is invalid: invalid value", true],
    [404, "Chat not found", true],
    [409, "Copilot proposal is already being accepted", false],
    [500, "Workflow changed after this proposal", false],
    [409, undefined, true],
  ])(
    "handles Accept HTTP %s %s with definitive release %s",
    async (status, detail, releases) => {
      pendingProposal();
      await renderChat();
      const accept = await screen.findByRole("button", { name: "Accept" });
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      if (detail === "Copilot proposal is already being accepted")
        historyResponse.data.proposed_claim_expires_in_seconds = 120;
      cancelPost.mockRejectedValueOnce({
        response: {
          status,
          data: detail === undefined ? undefined : { detail },
        },
      });
      await act(async () => fireEvent.click(accept));
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance === null,
      ).toBe(releases);
      expect(
        sessionStorage.getItem("copilot-pending-accept:wpid_1") === null,
      ).toBe(releases);
      if (releases) {
        if (
          detail !== undefined &&
          detail !== "Copilot proposal changed; reload required"
        )
          expect(vi.mocked(toast)).toHaveBeenCalledWith(
            expect.objectContaining({
              title: "Accept failed",
              description: detail,
            }),
          );
        else expect(screen.getByText("Not saved")).toBeTruthy();
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        expect(
          cancelPost.mock.calls.some(
            ([path]) => path === "/workflow/copilot/clear-proposed-workflow",
          ),
        ).toBe(true);
        expect(beginSaveTransaction(owner)).toBe(true);
      } else expect(beginSaveTransaction(owner)).toBe(false);
    },
  );

  it("A46 preserves a description edited after the proposal and marks it dirty", async () => {
    pendingProposal();
    const saved = { ...saveData.workflow, description: "Proposal description" };
    const apply = vi.fn((workflow: WorkflowApiResponse) => {
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromWorkflow(workflow.description);
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    });
    await renderChat({ onWorkflowUpdate: apply });
    const accept = await screen.findByRole("button", { name: "Accept" });
    useWorkflowTitleStore
      .getState()
      .setDescriptionFromUser("Edited after proposal");
    saveData.description = "Edited after proposal";
    cancelPost.mockResolvedValueOnce({ data: saved });
    await act(async () => fireEvent.click(accept));
    expect(useWorkflowTitleStore.getState().description).toBe(
      "Edited after proposal",
    );
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });
});

async function checkSerializedAcceptRecovery(
  first: "aborted" | "failed" | "live",
) {
  historyResponse.data.proposed_workflow = proposedWorkflowPayload();
  const apply = vi.fn();
  await renderChat({ onWorkflowUpdate: apply });
  vi.useFakeTimers();
  const reads: Array<{
    resolve: (value: unknown) => void;
    reject: (error: unknown) => void;
  }> = [];
  let activeReads = 0;
  let maximumActiveReads = 0;
  historyGet.mockImplementation(
    (path: string, config?: { signal?: AbortSignal }) => {
      if (path === "/workflows/wpid_1")
        return Promise.resolve({
          data: { ...saveData.workflow, workflow_id: "wf_recovered" },
        });
      return new Promise((resolve, reject) => {
        activeReads += 1;
        maximumActiveReads = Math.max(maximumActiveReads, activeReads);
        let settled = false;
        const finish = (callback: (value: unknown) => void, value: unknown) => {
          if (settled) return;
          settled = true;
          activeReads -= 1;
          callback(value);
        };
        reads.push({
          resolve: (value) => finish(resolve, value),
          reject: (error) => finish(reject, error),
        });
        config?.signal?.addEventListener(
          "abort",
          () => finish(reject, new Error("Read cancelled")),
          { once: true },
        );
      });
    },
  );
  cancelPost.mockRejectedValueOnce(new Error("Response lost"));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(reads).toHaveLength(1);
  if (first !== "aborted") {
    await act(async () => {
      if (first === "failed") reads[0]!.reject(new Error("Read failed"));
      else
        reads[0]!.resolve({
          data: {
            ...historyResponse.data,
            proposed_claim_expires_in_seconds: 300,
          },
        });
    });
  }
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(reads).toHaveLength(2);
  expect(maximumActiveReads).toBe(1);
  expect(activeReads).toBe(1);
  expect(
    useWorkflowYamlEditorStore.getState().copilotAcceptance,
  ).not.toBeNull();
  await act(async () => {
    reads[0]!.resolve({
      data: {
        ...historyResponse.data,
        proposed_workflow: null,
        proposed_claim_expires_in_seconds: null,
      },
    });
  });
  expect(apply).not.toHaveBeenCalled();
  await act(async () => {
    reads[1]!.resolve({
      data: {
        ...historyResponse.data,
        proposed_workflow: null,
        proposed_claim_expires_in_seconds: null,
      },
    });
  });
  expect(activeReads).toBe(0);
  expect(apply).toHaveBeenCalledTimes(1);
  expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  await act(async () => vi.advanceTimersByTimeAsync(20_000));
  expect(reads).toHaveLength(2);
  expect(apply).toHaveBeenCalledTimes(1);
}

it("serializes a retry during a scheduled read without applying its cancelled result", async () => {
  await checkSerializedAcceptRecovery("aborted");
});

it("retries after a failed scheduled read without stranding the reservation", async () => {
  await checkSerializedAcceptRecovery("failed");
});

it("retries after a live-claim read and applies the eventual outcome once", async () => {
  await checkSerializedAcceptRecovery("live");
});

it("locks the pending question card while an Accept is unresolved", async () => {
  // `handleQuestionAnswer` is fenced and returns false SILENTLY - no request, no toast. So a card
  // that stays clickable under an unresolved Accept reports a submit that never happened, which
  // is the false receipt this whole slice exists to remove, reached without the server at all.
  // The Cancel control beside it already gates on the same fence; this card was the outlier.
  historyResponse.data.question_interactions = [
    {
      interaction_id: "q-1",
      turn_id: "turn-1",
      tool_call_id: "ask-1",
      status: "pending",
      response: null,
      created_at: "2026-09-17T00:00:01Z",
      resolved_at: null,
      parts: [
        {
          part_id: "p-1",
          prompt: "Which one?",
          choices: [{ choice_id: "a", text: "Column A" }],
        },
      ],
    },
  ];
  // The proposal arrives by HYDRATION rather than the composer: a question card renders its own
  // textarea, and the shared `submit` helper resolves a single textbox by role.
  historyResponse.data.proposed_workflow = proposedWorkflowPayload();
  historyResponse.data.proposed_workflow_metadata = {
    owner_turn_id: "turn-1",
    revision: 1,
    canonical_fingerprint: "canonical-1",
    disposition: "review_untested",
    workflow_run_id: null,
  };
  const view = await renderChat();

  // SETUP ASSERTION: the card is actionable BEFORE the fence closes. Without this the assertion
  // below passes for a card that was never enabled, or never rendered at all.
  const skip = await screen.findByRole("button", { name: "Skip" });
  expect(skip.matches(":disabled")).toBe(false);

  view.unmount();
  historyResponse.data.proposed_workflow_metadata!.disposition = "accepting";
  historyResponse.data.proposed_claim_expires_in_seconds = 300;
  await renderChat();
  expect(await screen.findByText("Confirming\u2026")).toBeTruthy();

  // Both controls inert. Send is scoped to the card's own action row - the composer has a Send
  // too, and an unscoped query would assert against whichever came first.
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Skip" }).matches(":disabled"),
    ).toBe(true),
  );
  const actionRow = screen.getByRole("button", { name: "Skip" }).parentElement!
    .parentElement!;
  expect(
    within(actionRow)
      .getByRole("button", { name: "Send" })
      .matches(":disabled"),
  ).toBe(true);
  // And the hold names itself where the choice count used to be, so the card is distinguishable
  // from a broken one.
  expect(within(actionRow).queryByText(/choices selected/)).toBeNull();
  const writes = cancelPost.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  fireEvent.click(within(actionRow).getByRole("button", { name: "Send" }));
  expect(cancelPost).toHaveBeenCalledTimes(writes);
});

it("A46 describes a cleared proposal as a changed saved workflow without attributing its writer", async () => {
  changesState.hasChanges = true;
  historyResponse.data.proposed_workflow = proposedWorkflowPayload();
  historyResponse.data.proposed_workflow_metadata = {
    owner_turn_id: "turn-claim",
    revision: 1,
    canonical_fingerprint: "before",
    disposition: "accepting",
    workflow_run_id: null,
  };
  historyResponse.data.proposed_claim_expires_in_seconds = 120;
  await renderChat();
  await act(async () => {});
  historyResponse.data.proposed_workflow = null;
  historyResponse.data.proposed_workflow_metadata = null;
  historyResponse.data.proposed_claim_expires_in_seconds = null;
  historyGet.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/workflows/wpid_1"
        ? { data: { ...saveData.workflow, version: 2 } }
        : historyResponse,
    ),
  );
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(
    screen.getByText(
      "The saved workflow changed while you had local edits. Choose which changes to keep.",
    ),
  ).toBeTruthy();
});

describe("editor-state rollback lifecycle", () => {
  it.each([false, true])(
    "settles an unchanged parked turn without claiming a save (local edits=%s)",
    async (localEdits) => {
      saveData.workflow = {
        ...saveData.workflow,
        title: saveData.title,
        version: 1,
        modified_at: "2026-09-11T00:00:00Z",
        workflow_definition: { version: 1, blocks: [], parameters: [] },
      };
      const first = await renderChat();
      await submit("edit the workflow");
      const call = streamCalls[0]!;
      vi.useFakeTimers();
      await act(async () => {
        call.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        call.reject(new Error("connection dropped"));
      });
      first.unmount();
      const parked = canonicalRecoveriesByWorkflow.get("wpid_1")!;
      expect(parked.rollback?.workflowPersisted).toBe(false);
      const other = await renderChat({ workflowPermanentId: "wpid_other" });
      other.unmount();
      const apply = vi.fn();
      const restore = vi.fn(restoreLive);
      await renderChat({
        onWorkflowUpdate: apply,
        onRestore: restore,
        beforeRecovery: () => {
          if (!localEdits) return;
          expect(refuseMutationDuringYamlCommit()).toBe(false);
          editorNodes = editorNodes.map((node) =>
            node.id === "loop"
              ? ({
                  ...node,
                  data: { ...node.data, loopValue: "fresh_local_items" },
                } as AppNode)
              : node,
          );
          useWorkflowHasChangesStore.getState().setHasChanges(true);
        },
      });
      const generation = useWorkflowHasChangesStore.getState().saveGeneration;
      finishTurnHistory();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(2_000);
      });
      if (localEdits) {
        expect(
          editorNodes.find((node) => node.id === "loop")?.data,
        ).toMatchObject({ loopValue: "fresh_local_items" });
        expect(restore).not.toHaveBeenCalled();
        expect(apply).not.toHaveBeenCalled();
        expect(
          screen.queryByRole("button", { name: "Keep my edits" }),
        ).toBeNull();
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      }
      expect(parked.rollback?.workflowPersisted).toBe(false);
      expect(useWorkflowHasChangesStore.getState().saveGeneration).toBe(
        generation,
      );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(toast).not.toHaveBeenCalledWith(
        expect.objectContaining({
          description: expect.stringContaining("saved on the server"),
        }),
      );
    },
  );

  it("explains why comparison Reject is blocked during an unresolved Accept", async () => {
    let reject!: () => Promise<boolean>;
    await renderChat({
      onReviewWorkflow: (_workflow, _clear, handler) => {
        reject = handler;
      },
    });
    const call = await stage();
    await act(async () => {
      call.onMessage(proposalResponse("Draft ready."));
      call.resolve();
    });
    cancelPost.mockImplementationOnce(() => new Promise(() => {}));
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Accept" })),
    );
    fireEvent.click(screen.getByRole("button", { name: /Review/ }));
    const version = {
      ...proposedWorkflowPayload(),
      version: 1,
    } as WorkflowVersion;
    const close = vi.fn();
    const panel = render(
      <WorkflowComparisonPanel
        version1={version}
        version2={version}
        mode="copilot"
        onCopilotReviewClose={bindCopilotReviewClose(reject, close)}
      />,
    );
    vi.mocked(toast).mockClear();
    await act(async () =>
      fireEvent.click(
        within(panel.container).getByRole("button", { name: "Reject" }),
      ),
    );
    expect(close).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Copilot is saving your accepted changes.",
      }),
    );
  });

  it.each(["queued", "auto"] as const)(
    "flushes the live default title before %s Send and keeps it after title_update",
    async (entry) => {
      saveData.workflow = { ...saveData.workflow, title: "New Workflow" };
      useWorkflowTitleStore.setState({
        title: "New Workflow",
        titleHasBeenGenerated: false,
      });
      useWorkflowHasChangesStore.getState().setGetSaveData(() => ({
        ...saveData,
        title: useWorkflowTitleStore.getState().title,
      }));
      const capture = vi.spyOn(editorStateSnapshots, "captureEditorState");
      const view = await renderChat({
        requiresLiveBrowser: entry === "queued",
        isLiveBrowserReady: entry !== "queued",
      });
      if (entry === "queued") await submit("Keep my code and update the title");
      function LiveTitle() {
        const title = useWorkflowTitleStore((state) => state.title);
        const { mutationLocked, onTitleChange } = useDeferredTitleEdit();
        return (
          <EditableNodeTitle
            value={title}
            editable={!mutationLocked}
            onChange={onTitleChange}
          />
        );
      }
      const title = render(
        <WorkflowPermanentIdContext.Provider value="wpid_1">
          <LiveTitle />
        </WorkflowPermanentIdContext.Provider>,
      );
      try {
        fireEvent.click(screen.getByRole("heading", { name: "New Workflow" }));
        fireEvent.change(screen.getByDisplayValue("New Workflow"), {
          target: { value: "User typed title" },
        });
        expect(useWorkflowTitleStore.getState().title).toBe("New Workflow");
        await act(async () => {
          if (entry === "queued") view.connectBrowser();
          else view.autoSend();
        });
        expect(streamCalls).toHaveLength(1);
        const call = streamCalls[0]!;
        await act(async () => {
          call.onMessage({
            type: "title_update",
            workflow_permanent_id: "wpid_1",
            title: "Copilot generated title",
          });
          call.onMessage(plainReplyResponse("Done."));
          call.resolve();
        });
        await waitFor(() =>
          expect(
            useWorkflowYamlEditorStore.getState().copilotAcceptance,
          ).toBeNull(),
        );
        expect(useWorkflowTitleStore.getState().title).toBe("User typed title");
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
        expect(parse(call.body.workflow_yaml).title).toBe("User typed title");
        expect(capture.mock.results[0]!.value).toMatchObject({
          title: "User typed title",
          hasChanges: true,
        });
        expect(deferredEdits.has("wpid_1:title")).toBe(false);
      } finally {
        title.unmount();
        view.unmount();
        capture.mockRestore();
      }
    },
  );

  it.each(["typed", "queued", "auto", "generate"] as const)(
    "flushes a buffered block before %s Send and preserves it through Accept",
    async (entry) => {
      const capture = vi.spyOn(editorStateSnapshots, "captureEditorState");
      const view = await renderChat({
        requiresLiveBrowser: entry === "queued",
        isLiveBrowserReady: entry !== "queued",
      });
      const composer = textarea();
      if (entry === "queued") await submit("Keep my code and update the title");
      function BufferedBlock() {
        const [text, setText] = useState("original code");
        useEffect(() => {
          editorNodes = [
            {
              id: "code",
              type: "codeBlock",
              position: { x: 0, y: 0 },
              data: { label: "code", code: text },
            },
          ] as AppNode[];
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
        return (
          <WorkflowBlockInputTextarea
            nodeId="code"
            name="code"
            aria-label="Buffered code"
            value={text}
            onChange={setText}
            hideActions
          />
        );
      }
      const block = render(
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId: "wpid_1", readOnly: false }}
          >
            <BufferedBlock />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>,
      );
      vi.useFakeTimers();
      try {
        fireEvent.change(
          screen.getByRole("textbox", { name: "Buffered code" }),
          { target: { value: "newest unblurred code" } },
        );
        expect(
          useWorkflowHasChangesStore.getState().getSaveData()!.blocks[0],
        ).toMatchObject({ code: "original code" });
        await act(async () => {
          if (entry === "queued") view.connectBrowser();
          else if (entry === "auto") view.autoSend();
          else if (entry === "generate")
            useCopilotActionStore.getState().requestBuild({
              blockLabel: "code",
              prompt: "Keep my code and update the title",
            });
          else {
            fireEvent.change(composer, {
              target: { value: "Keep my code and update the title" },
            });
            fireEvent.keyDown(composer, { key: "Enter" });
          }
        });
        expect(streamCalls).toHaveLength(1);
        const call = streamCalls[0]!;
        const document = parse(call.body.workflow_yaml);
        expect(document.workflow_definition.blocks[0]).toMatchObject({
          code: "newest unblurred code",
        });
        expect(capture.mock.results[0]!.value.nodes[0].data).toMatchObject({
          code: "newest unblurred code",
        });
        const accepted = {
          ...saveData.workflow,
          title: "Updated title",
          workflow_definition: document.workflow_definition,
        };
        await act(async () => {
          call.onMessage(
            proposalResponse("Ready", { updated_workflow: accepted }),
          );
          call.resolve();
        });
        cancelPost.mockResolvedValueOnce({ data: accepted });
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Accept" })),
        );
        await act(async () => vi.advanceTimersByTimeAsync(300));
        expect(
          editorNodes.find((node) => node.type === "codeBlock")?.data,
        ).toMatchObject({ code: "newest unblurred code" });
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
        expect(deferredEdits.size).toBe(0);
      } finally {
        block.unmount();
        view.unmount();
        capture.mockRestore();
        useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
        vi.useRealTimers();
      }
    },
  );

  it("keeps a standing YAML validation error through unchanged flush and Reject", async () => {
    await renderChat({
      onWorkflowUpdate: () => reconcileYamlDraftAfterGraphChange(),
    });
    const composer = textarea();
    const draft = "title: ''\nworkflow_definition:\n  blocks: []\n";
    act(() => {
      useWorkflowYamlEditorStore.getState().open(draft);
      useWorkflowYamlEditorStore.getState().setError("title must not be empty");
    });
    vi.stubGlobal("IntersectionObserver", undefined);
    vi.useFakeTimers();
    const editor = render(
      <WorkflowYamlEditor workflowId="wpid_1" variant="pane" />,
    );
    try {
      await act(async () => {
        fireEvent.change(composer, { target: { value: "edit the workflow" } });
        fireEvent.keyDown(composer, { key: "Enter" });
      });
      expect(useWorkflowYamlEditorStore.getState().error).toBe(
        "title must not be empty",
      );
      const call = streamCalls[0]!;
      await act(async () => {
        call.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        call.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        call.onMessage(proposalResponse("Ready"));
        call.resolve();
      });
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Reject" })),
      );
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        draft,
        error: "title must not be empty",
        stale: false,
      });
      expect(screen.getByRole("alert").textContent).toContain(
        "title must not be empty",
      );
    } finally {
      editor.unmount();
      vi.unstubAllGlobals();
    }
  });

  it.each(["nodes", "edges"] as const)(
    "restores parked recovery with only %s changed without graph authorship and records the next user edit",
    async (changedGraphPart) => {
      const canonical = {
        ...saveData.workflow,
        title: saveData.title,
        version: 1,
        workflow_definition: { version: 1, blocks: [], parameters: [] },
      };
      saveData.workflow = canonical;
      const view = await renderChat();
      if (changedGraphPart === "edges") {
        editorNodes.push({
          id: "code",
          type: "codeBlock",
          position: { x: 0, y: 0 },
          data: { label: "Code", code: "pass" },
        } as AppNode);
      }
      await stage();
      vi.useFakeTimers();
      view.unmount();
      expect(
        canonicalRecoveriesByWorkflow.get("wpid_1")?.rollback?.snapshot,
      ).toBeDefined();
      const RealChat = WorkflowCopilotChat;
      const chat = vi
        .spyOn(copilotChatModule, "WorkflowCopilotChat")
        .mockImplementation((props) => (
          <RealChat
            {...props}
            isOpen
            docked={false}
            requiresLiveBrowser={false}
          />
        ));
      const graphEdit = vi.spyOn(
        useWorkflowTitleStore.getState(),
        "recordCopilotGraphEdit",
      );
      const snapshot =
        canonicalRecoveriesByWorkflow.get("wpid_1")!.rollback!.snapshot!;
      const initial =
        changedGraphPart === "edges"
          ? {
              nodes: structuredClone(snapshot.nodes),
              edges: [{ id: "loop-code", source: "loop", target: "code" }],
            }
          : getElements([], saveData.settings, true);
      historyGet.mockImplementation((path: string) =>
        Promise.resolve({
          data:
            path === "/workflows/wpid_1"
              ? canonical
              : path.includes("copilot")
                ? historyResponse.data
                : [],
        }),
      );
      cancelPost.mockResolvedValue({ data: { blocks: [] } });
      vi.stubGlobal(
        "ResizeObserver",
        class {
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
      try {
        render(
          <QueryClientProvider client={queryClient}>
            <MemoryRouter>
              <TooltipProvider>
                <DebugStoreContext.Provider
                  value={{ isDebugMode: false, blockRunsEnabled: false }}
                >
                  <ReactFlowProvider>
                    <Workspace
                      workflow={canonical}
                      initialTitle={canonical.title}
                      initialNodes={initial.nodes}
                      initialEdges={initial.edges}
                      embedded
                    />
                  </ReactFlowProvider>
                </DebugStoreContext.Provider>
              </TooltipProvider>
            </MemoryRouter>
          </QueryClientProvider>,
        );
        await act(async () => {});
        act(() =>
          useWorkflowTitleStore
            .getState()
            .trackCopilotMetadata("wpid_1", "proposal-1"),
        );
        expect(
          useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_1
            ?.graphEdited,
        ).toBeFalsy();
        if (changedGraphPart === "edges") {
          const calls = vi.mocked(FlowRenderer).mock.calls;
          const canvas = calls[calls.length - 1]![0];
          expect(
            canvas.nodes.map(({ id, type, data }) => ({ id, type, data })),
          ).toEqual(
            snapshot.nodes.map(({ id, type, data }) => ({ id, type, data })),
          );
          expect(canvas.edges).toEqual(initial.edges);
          expect(getWorkflowBlocks(canvas.nodes, canvas.edges)).not.toEqual(
            getWorkflowBlocks(snapshot.nodes, snapshot.edges),
          );
        }
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        finishTurnHistory();
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
        const calls = vi.mocked(FlowRenderer).mock.calls;
        const canvas = calls[calls.length - 1]![0];
        expect(canvas.nodes.some((node) => node.id === "loop")).toBe(true);
        expect(graphEdit).not.toHaveBeenCalled();
        expect(
          useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_1
            ?.graphEdited,
        ).toBeFalsy();
        if (changedGraphPart === "edges") {
          expect(canvas.edges).toEqual(snapshot.edges);
          act(() => canvas.setEdges(initial.edges));
        } else {
          act(() =>
            canvas.setNodes(
              canvas.nodes.map((node) =>
                node.type === "loop"
                  ? {
                      ...node,
                      data: { ...node.data, loopValue: "user_edited_items" },
                    }
                  : node,
              ),
            ),
          );
        }
        expect(graphEdit).toHaveBeenCalledOnce();
        expect(
          useWorkflowTitleStore.getState().copilotMetadataEdits.wpid_1
            ?.graphEdited,
        ).toBe(true);
      } finally {
        cleanup();
        chat.mockRestore();
        graphEdit.mockRestore();
        vi.unstubAllGlobals();
      }
    },
  );
  it("restores unsaved parameters and metadata, the Studio dirty indicator and save summary after parked recovery Reject", async () => {
    saveData.workflow = {
      ...saveData.workflow,
      title: saveData.title,
      description: "Saved description",
      version: 1,
      modified_at: "2026-09-11T00:00:00Z",
      workflow_definition: { version: 1, blocks: [], parameters: [] },
    };
    const canonical = saveData.workflow;
    function useHydrateEditor(workflow: WorkflowApiResponse) {
      useHydrateWorkflowParameters(workflow, workflow.workflow_permanent_id);
      const locked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
      useEffect(() => {
        if (locked) return;
        const titles = useWorkflowTitleStore.getState();
        titles.initializeTitle(workflow.title, workflow.workflow_permanent_id);
        titles.initializeDescription(
          workflow.workflow_permanent_id,
          workflow.description,
        );
      }, [workflow, locked]);
      useEffect(
        () => () => {
          const titles = useWorkflowTitleStore.getState();
          titles.resetTitleSession(workflow.workflow_permanent_id);
          titles.resetDescriptionSession(workflow.workflow_permanent_id);
        },
        [workflow.workflow_permanent_id],
      );
    }
    const initialHydration = renderHook(() => useHydrateEditor(canonical));
    const unsavedParameters = [
      {
        parameterType: "context" as const,
        key: "context",
        sourceParameterKey: "unsaved_source",
      },
    ];
    act(() =>
      useWorkflowParametersStore
        .getState()
        .setParametersFromUser(unsavedParameters),
    );
    const view = await renderChat();
    saveData.description = "Saved description";
    useWorkflowSnapshotStore.getState().captureSnapshot();
    saveData.title = "Unsaved title";
    useWorkflowTitleStore.getState().setTitle(saveData.title);
    saveData.description = "Unsaved description";
    useWorkflowTitleStore
      .getState()
      .setDescriptionFromUser(saveData.description);
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
    const baseline = structuredClone(
      useWorkflowSnapshotStore.getState().snapshot,
    );
    await submit("edit the workflow");
    const call = streamCalls[0]!;
    await act(async () => {
      call.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      call.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
    });
    vi.useFakeTimers();
    view.unmount();
    initialHydration.unmount();
    useWorkflowParametersStore.getState().resetParametersSession("wpid_1");
    expect(
      useWorkflowParametersStore.getState().parametersWorkflowPermanentId,
    ).toBeNull();
    expect(
      canonicalRecoveriesByWorkflow.get("wpid_1")?.rollback?.snapshot
        ?.workflowSnapshot,
    ).toMatchObject({
      snapshot: baseline,
      contentDirty: true,
      userHasEdited: true,
    });
    useWorkflowSnapshotStore.getState().clearSnapshot();
    const other = await renderChat({ workflowPermanentId: "wpid_2" });
    const otherWorkflow = {
      ...canonical,
      workflow_permanent_id: "wpid_2",
      title: "Other workflow title",
      description: "Other workflow description",
    };
    const otherHydration = renderHook(() => useHydrateEditor(otherWorkflow));
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: otherWorkflow.title,
      description: otherWorkflow.description,
    });
    expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
    useWorkflowSnapshotStore.getState().captureSnapshot();
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
    other.unmount();
    otherHydration.unmount();
    useWorkflowParametersStore.getState().resetParametersSession("wpid_2");
    useWorkflowSnapshotStore.getState().clearSnapshot();
    const restore = vi.fn(restoreLive);
    await renderChat({ onRestore: restore });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    const resumedHydration = renderHook(() => useHydrateEditor(canonical));
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    finishTurnHistory();
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(restore).toHaveBeenCalled();
    expect(useWorkflowSnapshotStore.getState()).toMatchObject({
      snapshot: baseline,
      contentDirty: true,
      userHasEdited: true,
    });
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "Unsaved title",
      description: "Unsaved description",
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(useWorkflowParametersStore.getState().parameters).toEqual(
      unsavedParameters,
    );
    resumedHydration.unmount();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Save workflow (unsaved changes)" }),
    );
    expect(screen.getByRole("dialog").textContent).toContain("Description");
  });

  it.each(["typed", "queued"] as const)(
    "flushes unblurred YAML before a %s Copilot reservation and restores it on Reject",
    async (entry) => {
      changesState.hasChanges = true;
      const view = await renderChat({
        requiresLiveBrowser: entry === "queued",
        isLiveBrowserReady: entry !== "queued",
        onWorkflowUpdate: () => reconcileYamlDraftAfterGraphChange(),
      });
      const composer = textarea();
      if (entry === "queued") {
        await submit("edit the workflow");
        expect(streamCalls).toHaveLength(0);
      }
      const initial = "title: Original\nworkflow_definition:\n  blocks: []\n";
      const newest = initial.replace("Original", "Newest unblurred draft");
      act(() => useWorkflowYamlEditorStore.getState().open(initial));
      vi.stubGlobal("IntersectionObserver", undefined);
      const editor = render(
        <WorkflowYamlEditor workflowId="wpid_1" variant="pane" />,
      );
      const codeView = EditorView.findFromDOM(
        editor.container.querySelector<HTMLElement>(".cm-content")!,
      )!;
      vi.useFakeTimers();
      try {
        act(() =>
          codeView.dispatch({
            changes: { from: 0, to: codeView.state.doc.length, insert: newest },
          }),
        );
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(initial);
        await act(async () => {
          if (entry === "queued") view.connectBrowser();
          else {
            fireEvent.change(composer, {
              target: { value: "edit the workflow" },
            });
            fireEvent.keyDown(composer, { key: "Enter" });
          }
        });
        expect(streamCalls).toHaveLength(1);
        expect(useWorkflowYamlEditorStore.getState().draft).toBe(newest);
        const call = streamCalls[0]!;
        await act(async () => {
          call.onMessage({
            type: "turn_start",
            turn_id: "turn-1",
            mode: "build",
            turn_index: 0,
          });
          call.onMessage({
            type: "workflow_draft",
            block_labels: [],
            workflow: proposedWorkflowPayload(),
          });
          call.onMessage(proposalResponse("Draft ready"));
          call.resolve();
        });
        expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
          draft: newest,
          stale: true,
        });
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
        await act(async () => vi.advanceTimersByTimeAsync(300));
        expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
          draft: newest,
          entrySnapshot: initial,
          stale: false,
          copilotAcceptance: null,
        });
        expect(codeView.state.doc.toString()).toBe(newest);
        expect(deferredEdits.size).toBe(0);
      } finally {
        editor.unmount();
        view.unmount();
        clearDeferredEdits();
        vi.useRealTimers();
        vi.unstubAllGlobals();
      }
    },
  );
  async function confirmRecovery() {
    vi.useFakeTimers();
    finishTurnHistory();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
  }
  async function stage() {
    await submit("edit the workflow");
    const call = streamCalls[0]!;
    await act(async () => {
      call.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        mode: "build",
        turn_index: 0,
      });
      call.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: proposedWorkflowPayload(),
      });
    });
    expect(editorNodes.find((node) => node.id === "loop")).toBeUndefined();
    return call;
  }
  it.each(["Reject", "cancelled", "reconciliation", "recovery Reject"])(
    "restores the open YAML draft after %s and permits its commit",
    async (outcome) => {
      changesState.hasChanges = true;
      await renderChat({
        onWorkflowUpdate: () => reconcileYamlDraftAfterGraphChange(),
      });
      const entrySnapshot = stringify({
        title: "Original title",
        workflow_definition: { blocks: [], parameters: [] },
      });
      const draft = entrySnapshot.replace(
        "Original title",
        "Unsaved YAML title",
      );
      const originalYaml = {
        active: true,
        entrySnapshot,
        draft,
        stale: false,
        error: "Previous validation error",
      };
      act(() => {
        useWorkflowYamlEditorStore.getState().open(entrySnapshot);
        useWorkflowYamlEditorStore.getState().setDraft(draft);
        useWorkflowYamlEditorStore.getState().setError(originalYaml.error);
      });
      const commit = vi.fn(async () => {
        const state = useWorkflowYamlEditorStore.getState();
        const { metadataPatch } = yamlCommitInputs(
          parse(state.draft),
          state.draft,
        );
        useWorkflowTitleStore.getState().setTitle(metadataPatch.title!);
        state.close();
        return true;
      });
      useWorkflowYamlEditorStore.getState().registerCommit(commit);
      const call = await stage();
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        draft,
        entrySnapshot,
        stale: true,
      });
      await act(async () => expect(await commitYamlDraft(false)).toBe(false));
      expect(commit).not.toHaveBeenCalled();
      if (outcome === "Reject") {
        await act(async () => {
          call.onMessage(proposalResponse("Draft ready"));
          call.resolve();
        });
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Reject" })),
        );
      } else {
        vi.useFakeTimers();
        if (outcome === "recovery Reject") {
          await act(async () => call.reject(new Error("connection dropped")));
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "Reject" })),
          );
          expect(cancelPost).toHaveBeenCalledWith(
            "/workflow/copilot/cancel",
            {
              cancel_token: call.body.cancel_token,
              workflow_copilot_chat_id: "chat-1",
              source: "stop_button",
            },
            { timeout: 5_000 },
          );
        } else {
          await act(async () => {
            call.onMessage(
              outcome === "cancelled"
                ? proposalResponse("Cancelled", {
                    cancelled: true,
                    updated_workflow: null,
                    workflow_applied: false,
                  })
                : { type: "error", turn_id: "turn-1", error: "Failed" },
            );
            call.resolve();
          });
        }
        finishTurnHistory();
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
      }
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject(originalYaml);
      expect(
        editorNodes.find((node) => node.id === "loop")?.data,
      ).toMatchObject({
        loopValue: "unsaved_items",
      });
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      await act(async () => expect(await commitYamlDraft(false)).toBe(true));
      expect(commit).toHaveBeenCalledOnce();
      expect(useWorkflowTitleStore.getState().title).toBe("Unsaved YAML title");
    },
  );
  it.each(["cancelled", "error"])(
    "restores on %s terminal while owning the reservation",
    async (terminal) => {
      changesState.hasChanges = true;
      const restore = vi.fn((snapshot: EditorStateSnapshot) => {
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        return restoreLive(snapshot);
      });
      await renderChat({ onRestore: restore });
      const call = await stage();
      vi.useFakeTimers();
      await act(async () => {
        call.onMessage(
          terminal === "cancelled"
            ? {
                ...proposalResponse("Cancelled"),
                cancelled: true,
                updated_workflow: null,
                workflow_applied: false,
              }
            : { type: "error", turn_id: "turn-1", error: "Failed" },
        );
        call.resolve();
      });
      expect(restore).not.toHaveBeenCalled();
      finishTurnHistory();
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(restore).toHaveBeenCalledOnce();
      expect(
        editorNodes.find((node) => node.id === "loop")?.data,
      ).toMatchObject({
        loopValue: "unsaved_items",
        loopVariableReference: "{{ item }}",
      });
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );
  it.each(["failed POST", "timeout"])(
    "restores a staged draft when Stop aborts after %s without a terminal frame",
    async (failure) => {
      changesState.hasChanges = true;
      await renderChat();
      await stage();
      vi.useFakeTimers();
      try {
        if (failure === "failed POST")
          cancelPost.mockRejectedValueOnce(new Error("Cancel unavailable"));
        await act(async () => {
          fireEvent.keyDown(textarea(), { key: "Escape" });
        });
        if (failure === "timeout") {
          expect(setEditorNodes).not.toHaveBeenCalled();
          await act(async () => vi.advanceTimersByTimeAsync(15_000));
        }
        expect(cancelPost).toHaveBeenCalledWith(
          "/workflow/copilot/cancel",
          expect.anything(),
          expect.objectContaining({
            timeout: 15_000,
            signal: expect.any(AbortSignal),
          }),
        );
        expect(setEditorNodes).not.toHaveBeenCalled();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).not.toBeNull();
        finishTurnHistory();
        (
          historyResponse.data.chat_history[0] as {
            turn_outcome: { terminal_reason: string };
          }
        ).turn_outcome.terminal_reason = "user_cancelled";
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
        expect(
          editorNodes.find((node) => node.id === "loop")?.data,
        ).toMatchObject({
          loopValue: "unsaved_items",
          loopVariableReference: "{{ item }}",
        });
        expect(setEditorNodes).toHaveBeenCalledOnce();
        expect(
          useWorkflowYamlEditorStore.getState().copilotAcceptance,
        ).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    },
  );
  it.each(["Retry", "Reject"])(
    "removes the recovery notice when %s resolves before the next poll tick",
    async (action) => {
      changesState.hasChanges = true;
      await renderChat();
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.reject(new Error("Canonical unavailable"))
          : Promise.resolve(historyResponse),
      );
      await submit("edit the workflow");
      if (action === "Reject") {
        await act(async () => {
          streamCalls[0]!.onMessage({
            type: "turn_start",
            turn_id: "turn-1",
            turn_index: 0,
          });
        });
      }
      vi.useFakeTimers();
      await act(async () =>
        streamCalls[0]!.reject(new Error("connection dropped")),
      );
      const notice =
        "The connection dropped, so Copilot is checking whether this turn finished.";
      expect(screen.getByText(notice)).toBeTruthy();
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? {
                data: {
                  ...saveData.workflow,
                  workflow_id: "wf_saved",
                  version: 2,
                },
              }
            : historyResponse,
        ),
      );
      if (action === "Retry") {
        historyResponse.data.request_turn_id = "turn-1";
        finishTurnHistory();
      }
      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", {
            name: action === "Retry" ? "Retry" : action,
          }),
        );
        await vi.advanceTimersByTimeAsync(0);
      });
      if (action === "Reject") {
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        finishTurnHistory();
        await act(async () => vi.advanceTimersByTimeAsync(5_000));
      }
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      historyGet.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(screen.queryByText(notice)).toBeNull();
      expect(historyGet).not.toHaveBeenCalled();
    },
  );

  it("stops rejected recovery after terminal history confirms unchanged canonical state", async () => {
    changesState.hasChanges = true;
    await renderChat();
    const call = await stage();
    vi.useFakeTimers();
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? Promise.reject(new Error("Canonical unavailable"))
        : Promise.resolve(historyResponse),
    );
    await act(async () => call.reject(new Error("connection dropped")));
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await confirmRecovery();
    expect(setEditorNodes).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    historyGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(historyGet).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "restores the snapshot on Reject after terminal history, a title-only write, and failed canonical read (dirty=%s)",
    async (hasChanges) => {
      changesState.hasChanges = true;
      useWorkflowTitleStore.setState({
        title: "New Workflow",
        titleHasBeenGenerated: false,
      });
      useWorkflowHasChangesStore.setState({ hasChanges });
      await renderChat({
        onWorkflowUpdate: (workflow) => {
          useWorkflowParametersStore.getState().setParameters([]);
          useWorkflowTitleStore
            .getState()
            .setDescriptionFromWorkflow(workflow.description);
          useWorkflowHasChangesStore.getState().setHasChanges(true);
        },
      });
      const snapshotNodes = structuredClone(editorNodes);
      const snapshotParameters = structuredClone(
        useWorkflowParametersStore.getState().parameters,
      );
      const snapshotDescription = useWorkflowTitleStore.getState().description;
      const owner =
        useWorkflowYamlEditorStore.getState().editorOwner ??
        createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.reject(new Error("Canonical unavailable"))
          : Promise.resolve(historyResponse),
      );
      await submit("edit the workflow");
      const call = streamCalls[0]!;
      await act(async () => {
        call.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        call.onMessage({
          type: "title_update",
          workflow_permanent_id: "wpid_1",
          title: "Saved name",
        });
        call.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
        call.resolve();
      });
      expect(editorNodes.find((node) => node.id === "loop")).toBeUndefined();
      expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
      expect(useWorkflowTitleStore.getState().description).toBe(
        "Proposed description",
      );
      expect(useWorkflowTitleStore.getState().title).toBe("Saved name");
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();

      await confirmRecovery();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).not.toBeNull();
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: { ...saveData.workflow, title: "Saved name" } }
            : historyResponse,
        ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Reject" }));
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(editorNodes).toMatchObject(snapshotNodes);
      expect(useWorkflowParametersStore.getState().parameters).toEqual(
        snapshotParameters,
      );
      expect(useWorkflowTitleStore.getState().description).toBe(
        snapshotDescription,
      );
      expect(useWorkflowTitleStore.getState().title).toBe("Saved name");
      expect(useWorkflowTitleStore.getState().titleHasBeenGenerated).toBe(true);
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(hasChanges);
      expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
      finishSaveTransaction(owner);
    },
  );

  it.each([
    { titleFrameReceived: true, definitionCommitted: false },
    { titleFrameReceived: false, definitionCommitted: false },
    { titleFrameReceived: true, definitionCommitted: true },
    { titleFrameReceived: false, definitionCommitted: true },
  ])(
    "reconciles a terminal title-only canonical advance without losing the snapshot (title frame=$titleFrameReceived, definition committed=$definitionCommitted)",
    async ({ titleFrameReceived, definitionCommitted }) => {
      changesState.hasChanges = true;
      saveData.workflow = {
        ...saveData.workflow,
        title: "New Workflow",
        version: 1,
        modified_at: "2026-09-11T00:00:00Z",
        workflow_definition: { blocks: [], parameters: [], version: 1 },
      };
      useWorkflowTitleStore.setState({
        title: "New Workflow",
        titleHasBeenGenerated: false,
      });
      const apply = vi.fn((workflow: WorkflowApiResponse) => {
        useWorkflowParametersStore.getState().setParameters([]);
        useWorkflowTitleStore
          .getState()
          .setDescriptionFromWorkflow(workflow.description);
        useWorkflowTitleStore
          .getState()
          .setTitleFromCopilotIfDefault(workflow.title);
      });
      const restore = vi.fn(restoreLive);
      const view = await renderChat({
        onWorkflowUpdate: apply,
        onRestore: restore,
      });
      const snapshotNodes = structuredClone(editorNodes);
      const snapshotParameters = structuredClone(
        useWorkflowParametersStore.getState().parameters,
      );
      const snapshotDescription = useWorkflowTitleStore.getState().description;
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.reject(new Error("Canonical unavailable"))
          : Promise.resolve(historyResponse),
      );
      await submit("edit the workflow");
      const call = streamCalls[0]!;
      await act(async () => {
        call.onMessage({
          type: "turn_start",
          turn_id: "turn-1",
          mode: "build",
          turn_index: 0,
        });
        if (titleFrameReceived) {
          call.onMessage({
            type: "title_update",
            workflow_permanent_id: "wpid_1",
            title: "Saved name",
          });
        }
        call.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: proposedWorkflowPayload(),
        });
        call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
        call.resolve();
      });
      expect(editorNodes.find((node) => node.id === "loop")).toBeUndefined();
      view.unmount();
      const recovery = canonicalRecoveriesByWorkflow.get("wpid_1")!;
      expect(recovery.rollback).toMatchObject({
        titlePersisted: titleFrameReceived,
        workflowPersisted: false,
      });
      await renderChat({ onWorkflowUpdate: apply, onRestore: restore });
      const canonical = {
        ...saveData.workflow,
        title: "Saved name",
        version: 2,
        modified_at: "2026-09-11T00:00:01Z",
        workflow_definition: {
          version: definitionCommitted ? 2 : 1,
          parameters: [],
          blocks: [],
        },
      };
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      apply.mockClear();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Reject" })),
      );
      await confirmRecovery();

      if (definitionCommitted) {
        expect(recovery.rollback?.workflowPersisted).toBe(true);
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
        expect(restore).not.toHaveBeenCalled();
      } else {
        expect(recovery.rollback).toMatchObject({
          titlePersisted: true,
          workflowPersisted: false,
        });
        expect(apply).not.toHaveBeenCalled();
        expect(restore).toHaveBeenCalledOnce();
        expect(editorNodes).toMatchObject(snapshotNodes);
        expect(useWorkflowParametersStore.getState().parameters).toEqual(
          snapshotParameters,
        );
        expect(useWorkflowTitleStore.getState()).toMatchObject({
          title: "Saved name",
          titleHasBeenGenerated: true,
          description: snapshotDescription,
        });
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    },
  );

  it("keeps a committed workflow on Reject after a failed canonical read", async () => {
    changesState.hasChanges = true;
    await renderChat();
    const call = await stage();
    const committedNodes = structuredClone(editorNodes);
    const owner =
      useWorkflowYamlEditorStore.getState().editorOwner ??
      createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? Promise.reject(new Error("Canonical unavailable"))
        : Promise.resolve(historyResponse),
    );
    await act(async () => {
      call.onMessage(
        proposalResponse("Cancelled", {
          cancelled: true,
          workflow_applied: true,
          proposal_disposition: "auto_applicable",
        }),
      );
      call.resolve();
    });
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();

    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(getWorkflowBlocks(editorNodes, [])).toEqual(
      getWorkflowBlocks(committedNodes, []),
    );
    expect(editorNodes.find((node) => node.id === "loop")).toBeUndefined();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(beginSaveTransaction(owner)).toBe(true);
    finishSaveTransaction(owner);
  });

  it.each([false, true])(
    "keeps failed canonical recovery actionable and blocks saves until resolution (persisted=%s)",
    async (persisted) => {
      changesState.hasChanges = true;
      const apply = vi.fn();
      await renderChat({ onWorkflowUpdate: apply });
      const call = await stage();
      const owner =
        useWorkflowYamlEditorStore.getState().editorOwner ??
        createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.reject(new Error("Canonical unavailable"))
          : Promise.resolve(historyResponse),
      );
      await act(async () => {
        if (persisted) {
          call.onMessage({
            type: "title_update",
            workflow_permanent_id: "wpid_1",
            title: "Saved name",
          });
          call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
          call.resolve();
        } else {
          call.reject(new Error("network error"));
        }
      });
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(setEditorNodes).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
      const readsBeforeRetry = historyGet.mock.calls.filter(
        ([path]) => path === "/workflows/wpid_1",
      ).length;
      vi.useFakeTimers();
      finishTurnHistory();
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Retry" })),
      );
      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(
        historyGet.mock.calls.filter(([path]) => path === "/workflows/wpid_1"),
      ).toHaveLength(readsBeforeRetry + 1);
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
      const canonical = persisted
        ? { ...proposedWorkflowPayload(), workflow_id: "wf_saved", version: 2 }
        : saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      apply.mockClear();
      await act(async () =>
        fireEvent.click(
          screen.getByRole("button", {
            name: persisted ? "Retry" : "Reject",
          }),
        ),
      );
      if (persisted) {
        expect(apply).not.toHaveBeenCalled();
        await confirmRecovery();
        expect(apply).toHaveBeenCalledExactlyOnceWith(
          canonical,
          expect.objectContaining({
            persisted: true,
            applied: true,
          }),
        );
        expect(setEditorNodes).not.toHaveBeenCalled();
      } else {
        expect(setEditorNodes).not.toHaveBeenCalled();
        expect(beginSaveTransaction(owner)).toBe(false);
        await confirmRecovery();
        expect(
          editorNodes.find((node) => node.id === "loop")?.data,
        ).toMatchObject({
          loopValue: "unsaved_items",
          loopVariableReference: "{{ item }}",
        });
        expect(setEditorNodes).toHaveBeenCalledOnce();
        expect(apply).not.toHaveBeenCalled();
      }
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
      expect(beginSaveTransaction(owner)).toBe(true);
      finishSaveTransaction(owner);
    },
  );
  it("restores unsaved edits when a persisted-title error retry reads unchanged canonical state", async () => {
    changesState.hasChanges = true;
    const savedAt = "2026-07-09T00:00:00Z";
    const canonical: WorkflowSaveData["workflow"] = {
      ...saveData.workflow,
      version: 1,
      modified_at: savedAt,
      description: "Saved description",
      workflow_definition: {
        parameters: [],
        blocks: [
          {
            block_type: "for_loop",
            label: "Loop",
            continue_on_failure: false,
            model: null,
            output_parameter: {
              parameter_type: "output",
              key: "Loop_output",
              output_parameter_id: "op_loop",
              workflow_id: saveData.workflow.workflow_id,
              description: null,
              created_at: savedAt,
              modified_at: savedAt,
              deleted_at: null,
            },
            loop_over: {
              parameter_type: "workflow",
              key: "saved_items",
              workflow_id: saveData.workflow.workflow_id,
              workflow_parameter_id: "wp_items",
              workflow_parameter_type: "json",
              default_value: [],
              description: null,
              created_at: savedAt,
              modified_at: savedAt,
              deleted_at: null,
            },
            loop_variable_reference: "{{ saved_item }}",
            loop_blocks: [],
            complete_if_empty: false,
          },
        ],
      },
    };
    useWorkflowHasChangesStore.setState({
      getSaveData: () => ({ ...saveData, workflow: canonical }),
    });
    await renderChat({
      onWorkflowUpdate: (workflow) =>
        useWorkflowTitleStore
          .getState()
          .setDescriptionFromWorkflow(workflow.description),
    });
    act(() => {
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromUser("User edited description");
      editorNodes = editorNodes.map((node) =>
        node.type === "loop"
          ? {
              ...node,
              data: {
                ...node.data,
                loopValue: "user_edited_items",
                loopVariableReference: "{{ user_edited_item }}",
              },
            }
          : node,
      );
    });
    const call = await stage();
    expect(useWorkflowTitleStore.getState().description).toBe(
      "Proposed description",
    );
    const owner =
      useWorkflowYamlEditorStore.getState().editorOwner ??
      createYamlCommitOwner("wpid_1");
    registerEditorOwner(owner);
    let canonicalReads = 0;
    historyGet.mockImplementation((path: string) => {
      if (path !== "/workflows/wpid_1") return Promise.resolve(historyResponse);
      canonicalReads += 1;
      return canonicalReads === 1
        ? Promise.reject(new Error("Canonical unavailable"))
        : Promise.resolve({ data: structuredClone(canonical) });
    });
    await act(async () => {
      call.onMessage({
        type: "title_update",
        workflow_permanent_id: "wpid_1",
        title: "Saved name",
      });
      call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
      call.resolve();
    });
    expect(canonicalReads).toBe(1);
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(beginSaveTransaction(owner)).toBe(false);
    expect(beginYamlCommit(owner)).toBe(false);
    vi.useFakeTimers();
    finishTurnHistory();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(canonicalReads).toBe(2);
    expect(
      editorNodes.find((node) => node.type === "loop")?.data,
    ).toMatchObject({
      loopValue: "user_edited_items",
      loopVariableReference: "{{ user_edited_item }}",
    });
    expect(useWorkflowTitleStore.getState().description).toBe(
      "User edited description",
    );
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(beginSaveTransaction(owner)).toBe(true);
    finishSaveTransaction(owner);
    expect(beginYamlCommit(owner)).toBe(true);
    finishYamlCommit(owner);
  });
  it("ignores a canonical retry result after the editor unmounts", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn();
    const view = await renderChat({ onWorkflowUpdate: apply });
    const call = await stage();
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? Promise.reject(new Error("Canonical unavailable"))
        : Promise.resolve(historyResponse),
    );
    await act(async () => {
      call.onMessage({
        type: "title_update",
        workflow_permanent_id: "wpid_1",
        title: "Saved name",
      });
      call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
      call.resolve();
    });
    let resolveRead!: (response: unknown) => void;
    historyGet.mockImplementation((path: string) =>
      path === "/workflows/wpid_1"
        ? new Promise((resolve) => {
            resolveRead = resolve;
          })
        : Promise.resolve(historyResponse),
    );
    vi.useFakeTimers();
    finishTurnHistory();
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    apply.mockClear();
    view.unmount();
    await act(async () => resolveRead({ data: proposedWorkflowPayload() }));
    expect(apply).not.toHaveBeenCalled();
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });
  it("comparison Reject uses the chat's real snapshot restore", async () => {
    changesState.hasChanges = true;
    let reject!: () => Promise<boolean>;
    await renderChat({
      onReviewWorkflow: (_workflow, _clear, handler) => {
        reject = handler;
      },
    });
    const call = await stage();
    await act(async () => {
      call.onMessage(proposalResponse("Draft ready."));
      call.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: /Review/ }));
    const version = {
      ...proposedWorkflowPayload(),
      version: 1,
    } as WorkflowVersion;
    const close = vi.fn();
    const panel = render(
      <WorkflowComparisonPanel
        version1={version}
        version2={version}
        mode="copilot"
        onCopilotReviewClose={bindCopilotReviewClose(reject, close)}
      />,
    );
    const owner =
      useWorkflowYamlEditorStore.getState().editorOwner ??
      createYamlCommitOwner("wpid_1");
    act(() => {
      expect(beginYamlCommit(owner)).toBe(true);
    });
    await act(async () => {
      fireEvent.click(
        within(panel.container).getByRole("button", { name: "Reject" }),
      );
    });
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
    act(() => finishYamlCommit(owner));
    await act(async () => {
      fireEvent.click(
        within(panel.container).getByRole("button", { name: "Reject" }),
      );
    });
    expect(editorNodes.find((node) => node.id === "loop")?.data).toMatchObject({
      loopValue: "unsaved_items",
    });
    expect(setEditorNodes).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledWith("reject");
  });
  it("a persisted title frame makes canonical reconciliation take precedence over cancellation", async () => {
    changesState.hasChanges = true;
    await renderChat();
    const call = await stage();
    const canonical = {
      ...proposedWorkflowPayload(),
      workflow_id: "wf_saved",
      version: 2,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await act(async () => {
      call.onMessage({
        type: "title_update",
        workflow_permanent_id: "wpid_1",
        title: "Saved name",
      });
      call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
      call.resolve();
    });
    expect(historyGet).toHaveBeenCalledWith(
      "/workflows/wpid_1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(setEditorNodes).not.toHaveBeenCalled();
    await confirmRecovery();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "The draft could not be restored" }),
    );
    expect(
      useWorkflowHasChangesStore.getState().saveGeneration,
    ).toBeGreaterThan(0);
  });
  it("reports a stale-workflow refusal during rollback and retains the reservation", async () => {
    changesState.hasChanges = true;
    const restore = vi
      .fn<(snapshot: EditorStateSnapshot) => RestoreResult>()
      .mockReturnValue("refused-stale-workflow");
    await renderChat({ onRestore: restore });
    const call = await stage();
    await act(async () => {
      call.onMessage({ type: "error", turn_id: "turn-1", error: "Failed" });
      call.resolve();
    });
    expect(restore).not.toHaveBeenCalled();
    await confirmRecovery();
    expect(restore).toHaveBeenCalledOnce();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "The draft could not be restored" }),
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    restore.mockImplementation(restoreLive);
    await confirmRecovery();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });
  it("reconciles a persisted auto-accept even when the terminal is cancelled", async () => {
    changesState.hasChanges = true;
    const apply = vi.fn();
    await renderChat({ onWorkflowUpdate: apply });
    const call = await stage();
    const canonical = {
      ...proposedWorkflowPayload(),
      workflow_id: "wf_saved",
      version: 2,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
      ),
    );
    await act(async () => {
      call.onMessage(
        proposalResponse("Cancelled", {
          cancelled: true,
          workflow_applied: true,
          proposal_disposition: "auto_applicable",
          updated_workflow: canonical as WorkflowApiResponse,
        }),
      );
      call.resolve();
    });
    expect(apply).toHaveBeenLastCalledWith(
      canonical,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(setEditorNodes).not.toHaveBeenCalled();
    expect(
      useWorkflowHasChangesStore.getState().saveGeneration,
    ).toBeGreaterThan(0);
  });
});
