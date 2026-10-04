import type { ComponentProps, ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
} from "./WorkflowCopilotChat";

import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { useRecordingRefinementEvidenceStore } from "@/store/RecordingRefinementEvidenceStore";
import {
  beginYamlCommit,
  createYamlCommitOwner,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type {
  QuestionInteraction,
  WorkflowCopilotCredentialRequiredUpdate,
} from "./workflowCopilotTypes";
import { ensureCredentialRecoveryToken } from "./credentialRecovery";

type StreamBody = {
  message: string;
  cancel_token?: string;
  supports_credential_pause?: boolean;
  supports_credential_pause_recovery?: boolean;
  supports_credential_generation?: boolean;
  credential_recovery_token?: string;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const {
  streamCalls,
  postStreaming,
  apiPost,
  sansApiPost,
  getClientMock,
  apiGet,
  historyResponse,
  credentialsData,
  credsFail,
  modalOverrideType,
  modalDefaultTestUrl,
  modalEditingCredentialId,
  modalDefaultTotpType,
  toastFn,
} = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const streaming = vi.fn(
    (
      _path: string,
      body: StreamBody,
      onMessage: (payload: unknown) => boolean,
    ) =>
      new Promise<void>((resolve, reject) => {
        calls.push({ body, onMessage, resolve, reject });
      }),
  );
  const history = {
    data: {
      workflow_copilot_chat_id: "chat-1" as string | null,
      request_turn_id: undefined as string | null | undefined,
      chat_history: [] as unknown[],
      proposed_workflow: null as Record<string, unknown> | null,
      proposed_claim_expires_in_seconds: null as number | null | undefined,
      proposed_workflow_metadata: null as {
        owner_turn_id: string;
        revision: number;
        canonical_fingerprint: string;
        disposition: "accepting";
        workflow_run_id: string | null;
      } | null,
      auto_accept: false,
      pending_credential_requests:
        [] as WorkflowCopilotCredentialRequiredUpdate[],
      question_interactions: [] as QuestionInteraction[],
      pending_question_cancel_token: null as string | null,
    },
  };
  const creds = {
    current: [] as Array<{
      credential_id: string;
      name: string;
      tested_url: string | null;
    }>,
  };
  const post = vi.fn().mockResolvedValue({});
  const sansPost = vi.fn().mockResolvedValue({});
  const fail = { current: false };
  const get = vi.fn().mockImplementation((path: string) => {
    if (path === "/credentials") {
      return fail.current
        ? Promise.reject(new Error("network"))
        : Promise.resolve({ data: creds.current });
    }
    if (path.startsWith("/credentials/")) {
      const id = decodeURIComponent(path.slice("/credentials/".length));
      return Promise.resolve({
        data: creds.current.find((c) => c.credential_id === id),
      });
    }
    return Promise.resolve(history);
  });
  // Route post by API version: copilot routes (credential-response) must use
  // the sans-api-v1 client (base_router), not the default /api/v1 client.
  const getClientFn = vi.fn((_cg: unknown, version?: string) =>
    Promise.resolve({
      get,
      post: version === "sans-api-v1" ? sansPost : post,
    }),
  );
  return {
    streamCalls: calls,
    postStreaming: streaming,
    apiPost: post,
    sansApiPost: sansPost,
    getClientMock: getClientFn,
    apiGet: get,
    historyResponse: history,
    credentialsData: creds,
    credsFail: fail,
    modalOverrideType: { current: undefined as string | undefined },
    modalDefaultTestUrl: { current: undefined as string | undefined },
    modalEditingCredentialId: { current: undefined as string | undefined },
    modalDefaultTotpType: { current: undefined as string | undefined },
    toastFn: vi.fn(),
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: getClientMock,
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: toastFn }));

// Radix Popover + cmdk misbehave in jsdom; stub them so the picker's items are clickable buttons.
// Unlike the card unit test's always-render stub, this one honors `open` and wires the trigger —
// WorkflowCopilotHistory also renders a Popover whose (closed) content pulls react-query, so an
// unconditional PopoverContent would force-mount it and crash with "No QueryClient".
vi.mock("@/components/ui/popover", async () => {
  const React = await import("react");
  const OpenCtx = React.createContext<{
    open: boolean;
    setOpen: (value: boolean) => void;
  }>({ open: false, setOpen: () => {} });
  return {
    Popover: ({
      open,
      onOpenChange,
      children,
    }: {
      open?: boolean;
      onOpenChange?: (value: boolean) => void;
      children?: ReactNode;
    }) => (
      <OpenCtx.Provider
        value={{ open: Boolean(open), setOpen: onOpenChange ?? (() => {}) }}
      >
        {children}
      </OpenCtx.Provider>
    ),
    PopoverTrigger: ({ children }: { children?: ReactNode }) => {
      const { open, setOpen } = React.useContext(OpenCtx);
      return <div onClick={() => setOpen(!open)}>{children}</div>;
    },
    PopoverContent: ({ children }: { children?: ReactNode }) => {
      const { open } = React.useContext(OpenCtx);
      return open ? <div>{children}</div> : null;
    },
  };
});
vi.mock("@/components/ui/command", () => ({
  Command: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  CommandInput: ({ placeholder }: { placeholder?: string }) => (
    <input placeholder={placeholder} />
  ),
  CommandList: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  CommandEmpty: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  CommandGroup: ({
    children,
    heading,
  }: {
    children?: ReactNode;
    heading?: string;
  }) => (
    <div>
      {heading ? <div>{heading}</div> : null}
      {children}
    </div>
  ),
  CommandItem: ({
    children,
    onSelect,
  }: {
    children?: ReactNode;
    onSelect?: () => void;
    value?: string;
  }) => (
    <button type="button" onClick={() => onSelect?.()}>
      {children}
    </button>
  ),
}));

// The real modal pulls react-query; a stub is enough to prove the connect →
// modal → onCredentialCreated wiring.
vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: ({
    isOpen,
    onCredentialCreated,
    overrideType,
    defaultTestUrl,
    editingCredential,
    defaultTotpType,
  }: {
    isOpen?: boolean;
    onCredentialCreated?: (id: string, name?: string) => void;
    overrideType?: string;
    defaultTestUrl?: string;
    editingCredential?: { credential_id: string };
    defaultTotpType?: string;
  }) => {
    modalOverrideType.current = overrideType;
    modalDefaultTestUrl.current = defaultTestUrl;
    modalEditingCredentialId.current = editingCredential?.credential_id;
    modalDefaultTotpType.current = defaultTotpType;
    return isOpen ? (
      <button
        type="button"
        data-testid="mock-create-credential"
        onClick={() => onCredentialCreated?.("new-cred-1", "New Login")}
      >
        create credential
      </button>
    ) : null;
  },
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
      pathname: "/",
      search: "",
      hash: "",
      state: null,
      key: "default",
    }),
  };
});

const saveData = {
  title: "Test WF",
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
};

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: Object.assign(
    () => ({ getSaveData: () => saveData }),
    {
      getState: () => ({
        hasChanges: true,
        setHasChanges: vi.fn(),
        // The Accept fence publishes its reason here; this harness arms the fence, so the
        // setter has to exist or the component throws on hydration.
        setSaveBlockedReason: () => {},
      }),
    },
  ),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

async function renderChat(
  props: ComponentProps<typeof WorkflowCopilotChat> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <WorkflowCopilotChat docked={false} {...props} />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
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

const turnStart = (turnId = "turn-1") => ({
  type: "turn_start",
  turn_id: turnId,
  turn_index: 0,
  mode: "build",
  timestamp: "2026-07-13T00:00:00Z",
});

const credentialFrame = (
  overrides: Partial<WorkflowCopilotCredentialRequiredUpdate> = {},
): WorkflowCopilotCredentialRequiredUpdate => ({
  type: "credential_required",
  turn_id: "turn-1",
  workflow_copilot_chat_id: "chat-1",
  resume_token: "rt-abc",
  reason: "workflow_credential_inputs_unbound",
  message: "",
  login_page_urls: ["https://news.ycombinator.com/login"],
  credential_refs: [],
  timeout_seconds: 300,
  // Future relative to the real clock so the inline countdown isn't expired
  // (an expired card disables its buttons).
  expires_at: new Date(Date.now() + 300_000).toISOString(),
  timestamp: new Date().toISOString(),
  ...overrides,
});

const terminalPromptResponse = (turnId = "turn-9") => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Connect a credential to continue.",
  updated_workflow: null,
  response_time: "2026-07-13T00:00:05Z",
  proposal_disposition: "no_proposal",
  turn_id: turnId,
  narrative_payload: {
    turnId,
    turnIndex: 0,
    mode: "build",
    responseType: "REPLY",
    terminal: "response",
    terminalMessage: "Connect a credential to continue.",
    narrativeSummary: "Connect a credential to continue.",
    startedAt: "2026-07-13T00:00:00Z",
    endedAt: "2026-07-13T00:00:05Z",
    credentialPrompt: { reason: "credential_name_unresolved" },
  },
});

// A turn that silently auto-bound a credential (no ask): carries credentialAutoBound, no prompt/pause.
const terminalAutoBoundResponse = (turnId = "turn-9") => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Signing in with your saved credential.",
  updated_workflow: null,
  response_time: "2026-07-13T00:00:05Z",
  proposal_disposition: "no_proposal",
  turn_id: turnId,
  narrative_payload: {
    turnId,
    turnIndex: 0,
    mode: "build",
    responseType: "REPLY",
    terminal: "response",
    terminalMessage: "Signing in with your saved credential.",
    narrativeSummary: "Signing in with your saved credential.",
    startedAt: "2026-07-13T00:00:00Z",
    endedAt: "2026-07-13T00:00:05Z",
    credentialAutoBound: { credentialId: "cred_work", name: "Work login" },
  },
});

// A pause that engaged but never sent a frame — no card should render.
const terminalDeclinedResponse = (turnId = "turn-7") => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Let me know which credential to use.",
  updated_workflow: null,
  response_time: "2026-07-13T00:00:05Z",
  proposal_disposition: "no_proposal",
  turn_id: turnId,
  narrative_payload: {
    turnId,
    turnIndex: 0,
    mode: "build",
    responseType: "REPLY",
    terminal: "response",
    terminalMessage: "Let me know which credential to use.",
    narrativeSummary: "Let me know which credential to use.",
    startedAt: "2026-07-13T00:00:00Z",
    endedAt: "2026-07-13T00:00:05Z",
    credentialPrompt: { reason: "credential_name_unresolved" },
    credentialPause: { outcome: "declined" },
  },
});

const errorFrame = (turnId = "turn-1") => ({
  type: "error",
  error: "The turn failed.",
  turn_id: turnId,
});

// A continuation the user cancels: a terminal response flagged cancelled, which
// flows through handleResponse (not the error path).
const cancelledContinuationResponse = (turnId = "turn-10") => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Canceled.",
  updated_workflow: null,
  cancelled: true,
  response_time: "2026-07-13T00:00:05Z",
  proposal_disposition: "no_proposal",
  turn_id: turnId,
  narrative_payload: {
    turnId,
    turnIndex: 1,
    mode: "build",
    responseType: "REPLY",
    terminal: "response",
    terminalMessage: "Canceled.",
    narrativeSummary: "Canceled.",
    startedAt: "2026-07-13T00:00:00Z",
    endedAt: "2026-07-13T00:00:05Z",
  },
});

function credentialResponsePosts() {
  return sansApiPost.mock.calls.filter(
    (call) => call[0] === "/workflow/copilot/credential-response",
  );
}

function credentialsGets() {
  return apiGet.mock.calls.filter((call) => call[0] === "/credentials");
}

beforeEach(() => {
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  sessionStorage.clear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  apiPost.mockClear();
  apiPost.mockResolvedValue({});
  sansApiPost.mockClear();
  sansApiPost.mockResolvedValue({});
  apiGet.mockReset();
  apiGet.mockImplementation((path: string) => {
    if (path === "/credentials") {
      return credsFail.current
        ? Promise.reject(new Error("network"))
        : Promise.resolve({ data: credentialsData.current });
    }
    if (path.startsWith("/credentials/")) {
      const id = decodeURIComponent(path.slice("/credentials/".length));
      return Promise.resolve({
        data: credentialsData.current.find((c) => c.credential_id === id),
      });
    }
    return Promise.resolve(historyResponse);
  });
  useRecordingRefinementEvidenceStore.setState({ armed: null });
  toastFn.mockClear();
  credentialsData.current = [];
  credsFail.current = false;
  modalOverrideType.current = undefined;
  modalDefaultTestUrl.current = undefined;
  modalEditingCredentialId.current = undefined;
  modalDefaultTotpType.current = undefined;
  historyResponse.data = {
    workflow_copilot_chat_id: "chat-1",
    request_turn_id: undefined,
    chat_history: [],
    proposed_workflow: null,
    proposed_claim_expires_in_seconds: null,
    proposed_workflow_metadata: null,
    auto_accept: false,
    pending_credential_requests: [],
    question_interactions: [],
    pending_question_cancel_token: null,
  };
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

const scoutResult = () => ({
  type: "tool_result",
  tool_name: "navigate_browser",
  display_label: "Opening page",
  success: true,
  summary: "Opened the sign-in page",
  iteration: 0,
  tool_call_id: "tc-1",
});

const streamScoutTurn = async () => {
  await renderChat();
  await submit("build me a workflow");
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
  await act(async () => {
    streamCalls[0]!.onMessage(turnStart());
    streamCalls[0]!.onMessage({ type: "design_start" });
    streamCalls[0]!.onMessage(scoutResult());
  });
};

describe("WorkflowCopilotChat — activity log", () => {
  it("renders the flat log, not the retired phase rail", async () => {
    await streamScoutTurn();
    expect(screen.getByText("1 browser action")).toBeTruthy();
    expect(screen.queryByText("Explore site")).toBeNull();
  });
});

describe("WorkflowCopilotChat — credential receipt placement", () => {
  it("releases the dock and header flag when a docked sign-in request times out", async () => {
    await streamScoutTurn();
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]!.onMessage(
        credentialFrame({ anchor_tool_call_id: "tc-1" }),
      );
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("group", { name: "Sign-in request" })).toBeTruthy();
    expect(useCopilotHeaderStore.getState().attention).toBe("credential");

    // The server sends no frame on timeout, so the deadline alone has to release the dock.
    await act(async () => vi.advanceTimersByTimeAsync(300_001));
    expect(screen.queryByRole("group", { name: "Sign-in request" })).toBeNull();
    expect(screen.queryByText(/continue below/)).toBeNull();
    expect(screen.getAllByText("Timed out").length).toBeGreaterThan(0);
    expect(useCopilotHeaderStore.getState().attention).toBeNull();
  });

  it("docks a live sign-in request above the composer and hands the receipt back to the transcript", async () => {
    credentialsData.current = [
      { credential_id: "cred-hn", name: "HN Login", tested_url: null },
    ];
    await streamScoutTurn();
    await act(async () => {
      streamCalls[0]!.onMessage(
        credentialFrame({ anchor_tool_call_id: "tc-1" }),
      );
    });
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const tray = await screen.findByRole("group", { name: "Sign-in request" });
    const marker = screen.getByText(/continue below/);
    expect(
      precedes(
        document.querySelector('[data-activity-row-id="tc-1"]')!,
        marker,
      ),
    ).toBe(true);
    expect(precedes(marker, tray)).toBe(true);
    expect(
      precedes(
        tray,
        screen.getByRole("group", { name: "Copilot message composer" }),
      ),
    ).toBe(true);
    expect(screen.getAllByRole("button", { name: "Skip for now" })).toEqual([
      within(tray).getByRole("button", { name: "Skip for now" }),
    ]);
    expect(useCopilotHeaderStore.getState().attention).toBe("credential");

    // Minimized, the tray keeps its name on the control that restores it.
    fireEvent.click(
      within(tray).getByRole("button", { name: "Minimize sign-in request" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Show Sign-in request" }),
    );
    expect(screen.getByRole("group", { name: "Sign-in request" })).toBeTruthy();

    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "credential_pause_resolved",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "chat-1",
        resume_token: "rt-abc",
        outcome: "connected",
        credential_id: "cred-hn",
        name: "HN Login",
        timestamp: new Date().toISOString(),
      });
    });
    expect(await screen.findByText("Credential 'HN Login' added")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Sign-in request" })).toBeNull();
    expect(screen.queryByText(/continue below/)).toBeNull();
    expect(useCopilotHeaderStore.getState().attention).toBeNull();
  });

  it("keeps a mid-turn credential card after the row it was raised on, live and once the turn ends", async () => {
    credentialsData.current = [
      { credential_id: "cred-hn", name: "HN Login", tested_url: null },
    ];
    await streamScoutTurn();
    const write = {
      tool_name: "update_workflow",
      display_label: "Updating workflow",
      iteration: 1,
      tool_call_id: "tc-2",
    };
    await act(async () => {
      streamCalls[0]!.onMessage(
        credentialFrame({ anchor_tool_call_id: "tc-1" }),
      );
      streamCalls[0]!.onMessage({
        type: "credential_pause_resolved",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "chat-1",
        resume_token: "rt-abc",
        outcome: "connected",
        credential_id: "cred-hn",
        name: "HN Login",
        timestamp: new Date().toISOString(),
      });
      streamCalls[0]!.onMessage({
        type: "tool_call",
        tool_input: {},
        ...write,
      });
      streamCalls[0]!.onMessage({
        type: "tool_result",
        success: true,
        summary: "Saved the draft",
        ...write,
      });
    });
    const row = (id: string) =>
      document.querySelector(`[data-activity-row-id="${id}"]`)!;
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const liveReceipt = await screen.findByText("Credential 'HN Login' added");
    expect(precedes(row("tc-1"), liveReceipt)).toBe(true);
    expect(precedes(liveReceipt, row("tc-2"))).toBe(true);

    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-1",
        message: "Signed in and saved the draft.",
        updated_workflow: null,
        response_time: "2026-07-13T00:00:06Z",
        proposal_disposition: "no_proposal",
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          designEnded: true,
          draft: { blockCount: 1, blockLabels: ["sign_in"] },
          terminal: "response",
          terminalMessage: "Signed in and saved the draft.",
          narrativeSummary: "Signed in and saved the draft.",
          startedAt: "2026-07-13T00:00:00Z",
          endedAt: "2026-07-13T00:00:06Z",
          designActivity: [
            {
              ...scoutResult(),
              kind: "tool_result",
              id: "tr-tc-1",
              text: "Opened the sign-in page",
              toolName: "navigate_browser",
            },
            {
              kind: "tool_call",
              id: "tc-tc-2",
              text: "Updating workflow…",
              toolName: "update_workflow",
              iteration: 1,
            },
            {
              kind: "tool_result",
              id: "tr-tc-2",
              text: "Saved the draft",
              toolName: "update_workflow",
              iteration: 1,
              success: true,
            },
          ],
          credentialPause: {
            outcome: "connected",
            credentialId: "cred-hn",
            anchorToolCallId: "tc-1",
          },
        },
      });
      streamCalls[0]!.resolve();
    });
    const receipt = await screen.findByText("Credential 'HN Login' added");
    expect(screen.getAllByText("Credential 'HN Login' added")).toHaveLength(1);
    expect(
      precedes(receipt, screen.getByText("Signed in and saved the draft.")),
    ).toBe(true);

    // The finished turn folds its steps; the card stays visible, and opening
    // the fold puts it back after the row it was raised on.
    fireEvent.click(
      document.querySelector<HTMLButtonElement>("[data-activity-fold]")!,
    );
    const unfolded = screen.getByText("Credential 'HN Login' added");
    expect(screen.getAllByText("Credential 'HN Login' added")).toHaveLength(1);
    expect(precedes(row("tc-1"), unfolded)).toBe(true);
    expect(precedes(unfolded, row("tc-2"))).toBe(true);
  });
});

describe("WorkflowCopilotChat — question receipt placement", () => {
  it("keeps an answered question after the step that asked it while later steps run", async () => {
    await streamScoutTurn();
    const ask = {
      tool_name: "ask_user",
      display_label: "Asking you",
      iteration: 1,
      tool_call_id: "tc-ask",
    };
    const said = (iteration: number, narration: string, second: number) => ({
      type: "narration",
      narration,
      iteration,
      timestamp: `2026-07-13T00:00:0${second}Z`,
    });
    const question = {
      interaction_id: "qi-1",
      turn_id: "turn-1",
      tool_call_id: "tc-ask",
      parts: [
        {
          part_id: "p1",
          prompt: "Which structure?",
          choices: [{ choice_id: "c-llc", text: "LLC" }],
        },
      ],
      status: "pending" as const,
      response: null,
      created_at: "2026-07-13T00:00:02Z",
      resolved_at: null,
    };
    const later = {
      tool_name: "get_block_schema",
      display_label: "Checking workflow block options",
      iteration: 2,
      tool_call_id: "tc-later",
    };
    await act(async () => {
      streamCalls[0]!.onMessage(said(1, "Checking what the form needs.", 1));
      streamCalls[0]!.onMessage({
        type: "tool_call",
        tool_input: {},
        timestamp: "2026-07-13T00:00:02Z",
        ...ask,
      });
      streamCalls[0]!.onMessage({
        type: "question_required",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "chat-1",
        cancel_token: null,
        interactions: [question],
      });
      streamCalls[0]!.onMessage({
        type: "question_resolved",
        continued: true,
        interaction: {
          ...question,
          status: "resolved",
          response: {
            answers: [{ part_id: "p1", choice_id: "c-llc", text: null }],
            text: null,
            skipped: false,
          },
          resolved_at: "2026-07-13T00:00:03Z",
        },
      });
      streamCalls[0]!.onMessage({
        type: "tool_result",
        success: true,
        summary: "Answered",
        timestamp: "2026-07-13T00:00:04Z",
        ...ask,
      });
      streamCalls[0]!.onMessage(said(2, "Choosing the blocks.", 5));
      streamCalls[0]!.onMessage({
        type: "tool_call",
        tool_input: {},
        timestamp: "2026-07-13T00:00:06Z",
        ...later,
      });
    });
    // The full suite runs this file under load; the default 1s wait flakes.
    const receipt = await screen.findByText(
      "You answered 1 question",
      undefined,
      {
        timeout: 5000,
      },
    );
    const step = (reason: string) =>
      [...document.querySelectorAll("[data-activity-row-id]")].find((row) =>
        row.textContent?.includes(reason),
      );
    const precedes = (a: Node | undefined, b: Node | undefined) =>
      Boolean(
        a &&
        b &&
        a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    // The receipt renders before later steps finish revealing, so wait for the steps too.
    await waitFor(
      () => {
        expect(precedes(step("Checking what the form needs."), receipt)).toBe(
          true,
        );
        expect(precedes(receipt, step("Choosing the blocks."))).toBe(true);
      },
      { timeout: 5000 },
    );
  });
});

describe("WorkflowCopilotChat — saved turn placement", () => {
  it("places a plan and a question whose rows the saved turn no longer keeps by their call and time", async () => {
    const at = (s: number) => `2026-07-13T00:00:${String(s).padStart(2, "0")}Z`;
    const call = (id: string, toolName: string, s: number) => [
      {
        kind: "tool_call",
        id: `tc-${id}`,
        text: "…",
        toolName,
        iteration: s,
        timestamp: at(s),
      },
      {
        kind: "tool_result",
        id: `tr-${id}`,
        text: "OK",
        toolName,
        iteration: s,
        success: true,
        timestamp: at(s),
      },
    ];
    const said = (text: string, s: number) => ({
      kind: "narration",
      id: `n-${s}`,
      text,
      iteration: s,
      timestamp: at(s),
    });
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "Apply for it",
        turn_id: "turn-1",
        created_at: at(0),
      },
      {
        sender: "ai",
        content: "Stopped.",
        turn_id: "turn-1",
        created_at: at(30),
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "completed",
        },
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          designEnded: true,
          terminal: "response",
          terminalMessage: "Stopped.",
          narrativeSummary: "Stopped.",
          startedAt: at(0),
          endedAt: at(30),
          draft: { blockCount: 1, blockLabels: ["fill_form"] },
          blocks: [],
          // The ask_user call at 0:02 aged out of the saved activity.
          designActivity: [
            said("Opening the form.", 10),
            ...call("nav", "navigate_browser", 11),
            said("Writing the plan.", 12),
            ...call("plan", "set_work_plan", 13),
            said("Checking the next page.", 14),
            ...call("look", "evaluate", 15),
          ],
        },
      },
    ];
    (historyResponse.data as Record<string, unknown>).work_plan = [
      "Fill in the form",
    ];
    historyResponse.data.question_interactions = [
      {
        interaction_id: "qi-early",
        turn_id: "turn-1",
        tool_call_id: "tc-aged-out",
        parts: [
          {
            part_id: "p1",
            prompt: "Which structure?",
            choices: [{ choice_id: "c-llc", text: "LLC" }],
          },
        ],
        status: "resolved",
        response: {
          answers: [{ part_id: "p1", choice_id: "c-llc", text: null }],
          text: null,
          skipped: false,
        },
        created_at: at(2),
        resolved_at: at(3),
      },
    ];
    await renderChat();
    await screen.findByText("You answered 1 question");
    const fold = document.querySelector<HTMLButtonElement>(
      "[data-activity-fold]",
    )!;
    // Folded, the question asked before every kept step still reads first,
    // while the plan made mid-turn stays below the fold line.
    expect(
      Boolean(
        screen
          .getByText("You answered 1 question")
          .compareDocumentPosition(fold) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
    expect(
      Boolean(
        fold.compareDocumentPosition(screen.getByText("Fill in the form")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
    fireEvent.click(fold);
    const step = (reason: string) =>
      [...document.querySelectorAll("[data-activity-row-id]")].find((row) =>
        row.textContent?.includes(reason),
      )!;
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const question = screen.getByText("You answered 1 question");
    const plan = screen.getByText("Fill in the form");
    expect(screen.getAllByText("Fill in the form")).toHaveLength(1);
    expect(precedes(question, step("Opening the form."))).toBe(true);
    expect(precedes(step("Writing the plan."), plan)).toBe(true);
    expect(precedes(plan, step("Checking the next page."))).toBe(true);
  });
});

describe("WorkflowCopilotChat — saved plan attribution", () => {
  it("does not credit an older turn with the plan when a newer turn's steps were trimmed", async () => {
    const at = (s: number) =>
      `2026-07-13T00:${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}Z`;
    const pair = (id: string, toolName: string, s: number) => [
      {
        kind: "tool_call",
        id: `tc-${id}`,
        text: "…",
        toolName,
        iteration: s,
        timestamp: at(s),
      },
      {
        kind: "tool_result",
        id: `tr-${id}`,
        text: "OK",
        toolName,
        iteration: s,
        success: true,
        timestamp: at(s),
      },
    ];
    const turn = (
      id: string,
      start: number,
      reply: string,
      designActivity: unknown[],
    ) => [
      {
        sender: "user",
        content: `Ask ${id}`,
        turn_id: id,
        created_at: at(start),
      },
      {
        sender: "ai",
        content: reply,
        turn_id: id,
        created_at: at(start + 50),
        turn_outcome: { copilot_turn_id: id, terminal_reason: "completed" },
        narrative_payload: {
          turnId: id,
          terminal: "response",
          terminalMessage: reply,
          narrativeSummary: reply,
          startedAt: at(start),
          endedAt: at(start + 50),
          blocks: [],
          designActivity,
        },
      },
    ];
    historyResponse.data.chat_history = [
      ...turn(
        "turn-1",
        0,
        "First turn done.",
        pair("plan", "set_work_plan", 5),
      ),
      // At the 50-entry cap: this turn's own set_work_plan call may have aged out.
      ...turn(
        "turn-2",
        100,
        "Second turn done.",
        Array.from({ length: 25 }, (_, n) =>
          pair(`look-${n}`, "evaluate", 101 + n),
        ).flat(),
      ),
    ];
    (historyResponse.data as Record<string, unknown>).work_plan = [
      "Fill in the form",
    ];
    await renderChat();
    const plan = await screen.findByText("Fill in the form");
    const secondReply = screen.getByText("Second turn done.");
    expect(
      Boolean(
        secondReply.compareDocumentPosition(plan) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
  });
});

describe("WorkflowCopilotChat — saved plan clear", () => {
  it("shows a plan cleared by a turn saved without its plan", async () => {
    const at = (s: number) => `2026-07-13T00:00:${String(s).padStart(2, "0")}Z`;
    const planResult = (id: string, s: number) => ({
      kind: "tool_result",
      id: `tr-${id}`,
      text: "OK",
      toolName: "set_work_plan",
      iteration: s,
      success: true,
      timestamp: at(s),
    });
    const aiTurn = (
      id: string,
      start: number,
      reply: string,
      extra: Record<string, unknown>,
    ) => ({
      sender: "ai",
      content: reply,
      turn_id: id,
      created_at: at(start + 5),
      turn_outcome: { copilot_turn_id: id, terminal_reason: "completed" },
      narrative_payload: {
        turnId: id,
        terminal: "response",
        terminalMessage: reply,
        narrativeSummary: reply,
        startedAt: at(start),
        endedAt: at(start + 5),
        blocks: [],
        ...extra,
      },
    });
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "Plan it",
        turn_id: "turn-1",
        created_at: at(0),
      },
      aiTurn("turn-1", 0, "Planned.", {
        designActivity: [planResult("plan", 1)],
        workPlan: { toolCallId: "tc-plan", items: ["Fill in the form"] },
      }),
      {
        sender: "user",
        content: "Drop it",
        turn_id: "turn-2",
        created_at: at(10),
      },
      // Saved without its plan; its newest successful call cleared it.
      aiTurn("turn-2", 10, "Cleared.", {
        designActivity: [planResult("clear", 11)],
      }),
    ];
    (historyResponse.data as Record<string, unknown>).work_plan = [];
    await renderChat();
    expect(await screen.findByText("Plan cleared")).toBeTruthy();
  });
});

describe("WorkflowCopilotChat — credential card wiring", () => {
  it("sends the initial attachments once after the authoring hold clears", async () => {
    useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
    const onInitialMessageConsumed = vi.fn();
    await renderChat({
      initialMessage: "Build from the attached procedure",
      initialAttachments: [
        {
          file_id: "file-procedure",
          filename: "procedure.pdf",
          available: true,
        },
      ],
      onInitialMessageConsumed,
    });
    expect(onInitialMessageConsumed).not.toHaveBeenCalled();
    expect(streamCalls).toHaveLength(0);

    await act(async () => {
      useWorkflowYamlEditorStore.setState({ authoringInProgress: false });
    });
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0]?.body).toMatchObject({
      message: "Build from the attached procedure",
      attached_file_ids: ["file-procedure"],
    });
    expect(onInitialMessageConsumed).toHaveBeenCalledOnce();
  });

  it.each([
    { pause: "credential", adopted: false },
    { pause: "question", adopted: false },
    { pause: "credential", adopted: true },
    { pause: "question", adopted: true },
    { pause: "question-response-lost", adopted: false },
    { pause: "cancel-question", adopted: false },
    { pause: "new-chat", adopted: false },
  ])(
    "retains one reserved recording recovery after $pause resumption (adopted: $adopted)",
    async ({ pause, adopted }) => {
      const apply = vi.fn();
      let canonical = saveData.workflow;
      apiGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1"
            ? { data: canonical }
            : path === "/credentials"
              ? { data: credentialsData.current }
              : historyResponse,
        ),
      );
      useRecordingRefinementEvidenceStore.getState().set({
        nonce: "resume-recording",
        evidence: {
          schema_version: 1,
          recording: { browser_session_id: "pbs-resume" },
          actions: [{ action_id: "a001" }],
          deleted_action_ids: [],
          truncated_action_count: 0,
          provenance: { source: "browser_recording" },
        },
      });
      await renderChat({
        initialAction: { kind: "refine_recording", nonce: "resume-recording" },
        onWorkflowUpdate: apply,
      });
      await waitFor(() => expect(streamCalls).toHaveLength(1));
      vi.useFakeTimers();
      historyResponse.data.request_turn_id = null;
      await act(async () => {
        if (!adopted) streamCalls[0]!.onMessage(turnStart());
        streamCalls[0]!.reject(new Error("connection dropped"));
        await vi.advanceTimersByTimeAsync(0);
      });
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      expect(reservation).not.toBeNull();
      if (adopted) {
        historyResponse.data.request_turn_id = "turn-1";
        await act(async () => vi.advanceTimersByTimeAsync(2_000));
      }
      const question: QuestionInteraction = {
        interaction_id: "resume-question",
        turn_id: "turn-1",
        tool_call_id: "ask-resume",
        parts: [{ part_id: "url", prompt: "Which URL?", choices: [] }],
        status: "pending",
        response: null,
        created_at: new Date().toISOString(),
        resolved_at: null,
      };
      if (pause === "credential") {
        credentialsData.current = [
          {
            credential_id: "cred-resume",
            name: "Test login",
            tested_url: "https://example.test/login",
          },
        ];
        historyResponse.data.pending_credential_requests = [
          credentialFrame({ login_page_urls: ["https://example.test/login"] }),
        ];
      } else {
        historyResponse.data.question_interactions = [question];
        historyResponse.data.pending_question_cancel_token = "cancel-question";
      }
      await act(async () =>
        vi.advanceTimersByTimeAsync(adopted ? 3_000 : 2_000),
      );
      if (pause === "credential") {
        await act(async () => fireEvent.click(screen.getByRole("combobox")));
        await act(async () =>
          fireEvent.click(screen.getByRole("button", { name: "Test login" })),
        );
        expect(credentialResponsePosts()).toHaveLength(1);
        historyResponse.data.pending_credential_requests = [];
      } else {
        const resolved: QuestionInteraction = {
          ...question,
          status: "resolved",
          response: { text: "https://example.test" },
          resolved_at: new Date().toISOString(),
        };
        if (pause === "new-chat") {
          await act(async () =>
            fireEvent.click(screen.getByRole("button", { name: "New chat" })),
          );
        } else if (pause === "cancel-question") {
          historyResponse.data.question_interactions = [resolved];
          historyResponse.data.pending_question_cancel_token = null;
          await act(async () =>
            fireEvent.click(
              screen.getByRole("button", { name: "Cancel question" }),
            ),
          );
        } else {
          if (pause === "question-response-lost") {
            historyResponse.data.question_interactions = [resolved];
            sansApiPost.mockRejectedValueOnce(new Error("response lost"));
          } else {
            sansApiPost.mockResolvedValueOnce({ data: resolved });
          }
          fireEvent.change(screen.getByLabelText("Your response"), {
            target: { value: "https://example.test" },
          });
          await act(async () =>
            fireEvent.click(
              within(
                screen.getByRole("group", { name: "Question parts" }),
              ).getByRole("button", { name: "Send" }),
            ),
          );
          expect(sansApiPost).toHaveBeenCalledWith(
            "/workflow/copilot/question-response",
            expect.objectContaining({
              interaction_id: question.interaction_id,
            }),
          );
        }
        historyResponse.data.question_interactions = [resolved];
        historyResponse.data.pending_question_cancel_token = null;
      }
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      const owner = createYamlCommitOwner("wpid_1");
      expect(beginYamlCommit(owner)).toBe(false);
      apiGet.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(5_000));
      expect(
        apiGet.mock.calls.filter(
          ([path]) => path === "/workflow/copilot/chat-history",
        ),
      ).toHaveLength(1);
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(apply).not.toHaveBeenCalled();
      canonical = { ...saveData.workflow, workflow_id: "wf_committed" };
      historyResponse.data.chat_history = [
        {
          sender: "ai",
          content: "Recording changes saved.",
          created_at: new Date().toISOString(),
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-1",
            terminal: "response",
            draft: { blockCount: 1, blockLabels: ["Open page"] },
          },
        },
      ];
      await act(async () => vi.advanceTimersByTimeAsync(8_000));
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        canonical,
        expect.objectContaining({ persisted: true, applied: true }),
      );
      if (pause === "new-chat")
        expect(screen.queryByText("Recording changes saved.")).toBeNull();
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      expect(beginYamlCommit(owner)).toBe(true);
      finishYamlCommit(owner);
      apiGet.mockClear();
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(
        apiGet.mock.calls.filter(
          ([path]) => path === "/workflow/copilot/chat-history",
        ),
      ).toHaveLength(0);
      await submit("Another edit");
      expect(streamCalls).toHaveLength(2);
    },
  );

  it("consolidates a reserved request poll when credential resumption precedes alias adoption", async () => {
    let canonical = saveData.workflow;
    let resolveLookup: ((value: typeof historyResponse) => void) | undefined;
    let holdLookup = false;
    const apply = vi.fn();
    apiGet.mockImplementation(
      (
        path: string,
        config?: { params?: { request_cancel_token?: string } },
      ) => {
        if (path === "/workflows/wpid_1")
          return Promise.resolve({ data: canonical });
        if (path === "/credentials")
          return Promise.resolve({ data: credentialsData.current });
        if (
          holdLookup &&
          config?.params?.request_cancel_token &&
          !resolveLookup
        ) {
          return new Promise<typeof historyResponse>((resolve) => {
            resolveLookup = resolve;
          });
        }
        return Promise.resolve(historyResponse);
      },
    );
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "late-alias",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-alias" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });
    const view = await renderChat({
      initialAction: { kind: "refine_recording", nonce: "late-alias" },
      onWorkflowUpdate: apply,
    });
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    vi.useFakeTimers();
    historyResponse.data.request_turn_id = null;
    await act(async () => {
      streamCalls[0]!.reject(new Error("connection dropped"));
      await vi.advanceTimersByTimeAsync(0);
    });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();
    holdLookup = true;
    credentialsData.current = [
      {
        credential_id: "cred-alias",
        name: "Test login",
        tested_url: "https://example.test/login",
      },
    ];
    historyResponse.data.pending_credential_requests = [
      credentialFrame({ login_page_urls: ["https://example.test/login"] }),
    ];
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(resolveLookup).toBeDefined();
    await act(async () => fireEvent.click(screen.getByRole("combobox")));
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Test login" })),
    );
    expect(credentialResponsePosts()).toHaveLength(1);
    historyResponse.data.pending_credential_requests = [];
    historyResponse.data.request_turn_id = "turn-1";
    holdLookup = false;
    await act(async () => resolveLookup!(structuredClone(historyResponse)));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    apiGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(
      apiGet.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/chat-history",
      ),
    ).toHaveLength(1);
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(sansApiPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      {
        cancel_token: streamCalls[0]!.body.cancel_token,
        workflow_copilot_chat_id: "chat-1",
        source: "stop_button",
      },
      expect.anything(),
    );
    canonical = { ...saveData.workflow, workflow_id: "wf_alias_commit" };
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Saved.",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "completed",
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(apply).toHaveBeenCalledExactlyOnceWith(
      canonical,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    apiGet.mockClear();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("unlocks ordinary disconnected turns when terminal history confirms unchanged canonical", async () => {
    const apply = vi.fn();
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat({ onWorkflowUpdate: apply });
    await submit("Explain this workflow");
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.reject(new Error("connection dropped"));
    });
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "This workflow opens the page.",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "completed",
        },
      },
    ];
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(apply).not.toHaveBeenCalled();
    const owner = createYamlCommitOwner("wpid_1");
    expect(beginYamlCommit(owner)).toBe(true);
    finishYamlCommit(owner);
    await submit("Another edit");
    expect(streamCalls).toHaveLength(2);
  });

  it("keeps a credential pause recovered from history in ownership", async () => {
    ensureCredentialRecoveryToken("wpid_1");
    historyResponse.data.pending_credential_requests = [credentialFrame()];
    credentialsData.current = [
      {
        credential_id: "cred-hn",
        name: "HN Login",
        tested_url: "https://news.ycombinator.com/login",
      },
    ];

    await renderChat();
    expect(await screen.findByRole("combobox")).toBeTruthy();

    await submit("start a second turn");

    expect(postStreaming).not.toHaveBeenCalled();
  });

  it("recovers a credential card without SSE and resumes its original turn", async () => {
    const recoveryToken = ensureCredentialRecoveryToken("wpid_1");
    const question: QuestionInteraction = {
      interaction_id: "question-1",
      turn_id: "turn-1",
      tool_call_id: "ask-1",
      parts: [{ part_id: "url", prompt: "Sign-in URL?", choices: [] }],
      status: "pending",
      response: null,
      created_at: new Date().toISOString(),
      resolved_at: null,
    };
    historyResponse.data.question_interactions = [question];
    credentialsData.current = [
      {
        credential_id: "cred-hn",
        name: "HN Login",
        tested_url: "https://news.ycombinator.com/login",
      },
    ];
    await renderChat();
    expect(apiGet).toHaveBeenCalledWith(
      "/workflow/copilot/chat-history",
      expect.objectContaining({
        headers: {
          "X-Copilot-Credential-Recovery-Token": recoveryToken,
        },
      }),
    );
    const resolved: QuestionInteraction = {
      ...question,
      status: "resolved",
      response: { text: "https://news.ycombinator.com/login" },
      resolved_at: new Date().toISOString(),
    };
    sansApiPost.mockResolvedValueOnce({ data: resolved });
    historyResponse.data.question_interactions = [resolved];
    historyResponse.data.pending_credential_requests = [credentialFrame()];
    fireEvent.change(await screen.findByLabelText("Your response"), {
      target: { value: "https://news.ycombinator.com/login" },
    });
    fireEvent.click(
      within(screen.getByRole("group", { name: "Question parts" })).getByRole(
        "button",
        { name: "Send" },
      ),
    );
    const recoveredCredentialPicker = await screen.findByRole(
      "combobox",
      {},
      { timeout: 10000 },
    );
    await submit("start a different turn");
    expect(postStreaming).not.toHaveBeenCalled();
    fireEvent.click(recoveredCredentialPicker);
    fireEvent.click(await screen.findByRole("button", { name: "HN Login" }));
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      turn_id: "turn-1",
      workflow_copilot_chat_id: "chat-1",
      resume_token: "rt-abc",
      action: "connected",
      credential_id: "cred-hn",
    });
    expect(postStreaming).not.toHaveBeenCalled();
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : path === "/credentials"
            ? { data: credentialsData.current }
            : historyResponse,
      ),
    );
    historyResponse.data.pending_credential_requests = [];
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Recovered continuation completed",
        created_at: new Date().toISOString(),
        narrative_payload: { turnId: "turn-1", terminal: "response" },
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "completed",
        },
      },
    ];
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1), {
      timeout: 10000,
    });
    expect(apiGet).toHaveBeenCalledWith("/workflows/wpid_1", expect.anything());
  }, 15000);

  it("sends a recoverable credential capability with the request", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]!.body.supports_credential_pause).toBe(true);
    expect(streamCalls[0]!.body.supports_credential_pause_recovery).toBe(true);
    expect(streamCalls[0]!.body.credential_recovery_token).toMatch(
      /^[0-9a-f]{64}$/,
    );
  });

  it("renders the inline-pause card when a credential_required frame arrives", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    expect(
      screen.getByText(
        "Copilot needs to sign in to https://news.ycombinator.com",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
  });

  it("resolves the live card from the server's credential_pause_resolved frame with no local click", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    expect(screen.getByText(/^\d+:\d{2}$/)).toBeTruthy();

    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "credential_pause_resolved",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "chat-1",
        resume_token: "rt-abc",
        outcome: "connected",
        credential_id: "cred-hn",
        name: "HN Login",
        timestamp: new Date().toISOString(),
      });
    });

    expect(await screen.findByText("Credential 'HN Login' added")).toBeTruthy();
    expect(screen.queryByText(/^\d+:\d{2}$/)).toBeNull();
    expect(screen.queryByText(/left to connect/)).toBeNull();
    expect(credentialResponsePosts()).toHaveLength(0);

    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-1",
        message: "Connected. Continuing.",
        updated_workflow: null,
        response_time: "2026-07-13T00:00:06Z",
        proposal_disposition: "no_proposal",
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          responseType: "REPLY",
          terminal: "response",
          terminalMessage: "Connected. Continuing.",
          startedAt: "2026-07-13T00:00:00Z",
          endedAt: "2026-07-13T00:00:06Z",
          credentialPause: { outcome: "connected", credentialId: "cred-hn" },
        },
      });
      streamCalls[0]!.resolve();
    });
    expect(await screen.findByText("Credential 'HN Login' added")).toBeTruthy();
    expect(screen.queryByText("Credential added")).toBeNull();
  });

  it.each([["before"], ["after"]])(
    "a not_admitted verdict arriving %s the pick's POST resolves never reads as added",
    async (order) => {
      let releasePost = () => {};
      sansApiPost.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            releasePost = () => resolve({});
          }),
      );
      credentialsData.current = [
        { credential_id: "cred-hn", name: "HN Login", tested_url: null },
      ];
      await renderChat();
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(turnStart());
        streamCalls[0]!.onMessage(credentialFrame());
      });
      await act(async () => {
        fireEvent.click(await screen.findByRole("combobox"));
      });
      await act(async () => {
        fireEvent.click(
          await screen.findByRole("button", { name: "HN Login" }),
        );
      });
      await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));

      const rejected = () =>
        streamCalls[0]!.onMessage({
          type: "credential_pause_resolved",
          turn_id: "turn-1",
          workflow_copilot_chat_id: "chat-1",
          resume_token: "rt-abc",
          outcome: "not_admitted",
          credential_id: null,
          name: null,
          timestamp: new Date().toISOString(),
        });
      await act(async () => {
        if (order === "before") rejected();
        releasePost();
      });
      await act(async () => {
        if (order === "after") rejected();
      });
      expect(await screen.findByText(/Sign-in skipped/)).toBeTruthy();
      expect(screen.queryByText(/added/)).toBeNull();

      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "response",
          workflow_copilot_chat_id: "chat-1",
          message: "Could not use that credential.",
          updated_workflow: null,
          response_time: "2026-07-13T00:00:06Z",
          proposal_disposition: "no_proposal",
          turn_id: "turn-1",
          narrative_payload: {
            turnId: "turn-1",
            turnIndex: 0,
            mode: "build",
            responseType: "REPLY",
            terminal: "response",
            terminalMessage: "Could not use that credential.",
            startedAt: "2026-07-13T00:00:00Z",
            endedAt: "2026-07-13T00:00:06Z",
            credentialPause: { outcome: "not_admitted" },
          },
        });
        streamCalls[0]!.resolve();
      });
      expect(await screen.findByText(/Sign-in skipped/)).toBeTruthy();
      expect(screen.queryByText(/added/)).toBeNull();
    },
  );

  it("keeps a dropped credential-pause turn owned while recovery polls", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
      streamCalls[0]!.reject(new Error("stream disconnected"));
    });

    await submit("start a second turn");

    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("skip POSTs a credential-response with action skip", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    });
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    const skipBody = credentialResponsePosts()[0]![1] as Record<
      string,
      unknown
    >;
    expect(skipBody).toMatchObject({
      turn_id: "turn-1",
      workflow_copilot_chat_id: "chat-1",
      resume_token: "rt-abc",
      action: "skip",
    });
    expect(skipBody.credential_id).toBeUndefined();
    expect(await screen.findByText(/Sign-in skipped/)).toBeTruthy();
    expect(screen.queryByText(/Credential '.*' added/)).toBeNull();
    expect(screen.queryByText(/Credential added/)).toBeNull();
  });

  it.each([
    ["unknown", /The vault didn't confirm the save/],
    ["created_not_connected", /Saved as Portal test account, not connected/],
  ] as const)(
    "registration Generate and save POSTs only the pause ids and a %s save removes the button",
    async (generateResult, message) => {
      sansApiPost.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflow/copilot/credential-generate"
            ? { data: { result: generateResult } }
            : {},
        ),
      );
      await renderChat();
      await submit("create a test account");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      expect(streamCalls[0]!.body.supports_credential_generation).toBe(true);
      await act(async () => {
        streamCalls[0]!.onMessage(turnStart());
        streamCalls[0]!.onMessage(
          credentialFrame({
            reason: "credential_registration",
            login_page_urls: ["https://portal.example.com/signup"],
            registration: {
              username: "tester@example.com",
              credential_name: "Portal test account",
            },
          }),
        );
      });
      await act(async () => {
        fireEvent.click(
          screen.getByRole("button", { name: "Generate and save" }),
        );
      });
      const generatePosts = () =>
        sansApiPost.mock.calls.filter(
          (call) => call[0] === "/workflow/copilot/credential-generate",
        );
      await waitFor(() => expect(generatePosts()).toHaveLength(1));
      expect(generatePosts()[0]![1]).toEqual({
        turn_id: "turn-1",
        workflow_copilot_chat_id: "chat-1",
        resume_token: "rt-abc",
      });
      expect(await screen.findAllByText(message)).not.toHaveLength(0);
      expect(
        screen.queryByRole("button", { name: "Generate and save" }),
      ).toBeNull();
      expect(credentialResponsePosts()).toHaveLength(0);
      expect(
        screen.queryByRole("button", { name: "Skip for now" }) === null,
      ).toBe(generateResult === "created_not_connected");
    },
  );

  const raiseRegistrationCard = async () => {
    await renderChat();
    await submit("create a test account");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(
        credentialFrame({
          reason: "credential_registration",
          login_page_urls: ["https://portal.example.com/signup"],
          registration: {
            username: "tester@example.com",
            credential_name: "Portal test account",
          },
        }),
      );
    });
    await waitFor(() => expect(credentialsGets().length).toBeGreaterThan(0));
  };

  it("registration unknown reloads the saved logins so a save that landed late can be picked", async () => {
    const extendedDeadline = new Date(Date.now() + 600_000).toISOString();
    sansApiPost.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflow/copilot/credential-generate"
          ? {
              data: {
                result: "unknown",
                expires_at: extendedDeadline,
              },
            }
          : {},
      ),
    );
    await raiseRegistrationCard();
    const listsBefore = credentialsGets().length;
    credentialsData.current = [
      {
        credential_id: "cred-new",
        name: "Portal test account",
        tested_url: null,
      },
    ];
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Generate and save" }),
      );
    });

    await waitFor(() =>
      expect(credentialsGets().length).toBeGreaterThan(listsBefore),
    );
    expect(
      (
        await screen.findAllByText(
          /didn't confirm the save. Check your credentials/,
        )
      ).length,
    ).toBeGreaterThan(0);
    expect((await screen.findAllByText("Saved logins")).length).toBeGreaterThan(
      0,
    );
    expect(credentialResponsePosts()).toHaveLength(0);
  });

  it("registration Generate whose POST fails reads the card back instead of offering a retry", async () => {
    sansApiPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/credential-generate"
        ? Promise.reject(new Error("gateway timeout"))
        : Promise.resolve({}),
    );
    await raiseRegistrationCard();
    historyResponse.data = {
      ...historyResponse.data,
      pending_credential_requests: [
        credentialFrame({
          reason: "credential_registration",
          login_page_urls: ["https://portal.example.com/signup"],
          registration: {
            username: "tester@example.com",
            credential_name: "Portal test account",
            attempted: true,
            outcome: "unknown",
          },
        }),
      ],
    };
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Generate and save" }),
      );
    });

    expect(
      (await screen.findAllByText(/The vault didn't confirm the save/)).length,
    ).toBeGreaterThan(0);
    expect(
      apiGet.mock.calls.some(
        ([path]) => path === "/workflow/copilot/chat-history",
      ),
    ).toBe(true);
    expect(
      screen.queryByRole("button", { name: "Generate and save" }),
    ).toBeNull();
    expect(toastFn).not.toHaveBeenCalled();
  });

  it("registration Generate whose POST fails keeps reading the card until the save settles", async () => {
    sansApiPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/credential-generate"
        ? Promise.reject(new Error("gateway timeout"))
        : Promise.resolve({}),
    );
    await raiseRegistrationCard();
    const registrationCard = (
      registration: Partial<
        NonNullable<WorkflowCopilotCredentialRequiredUpdate["registration"]>
      >,
      expiresAt?: string,
    ) =>
      credentialFrame({
        reason: "credential_registration",
        login_page_urls: ["https://portal.example.com/signup"],
        registration: {
          username: "tester@example.com",
          credential_name: "Portal test account",
          attempted: true,
          ...registration,
        },
        ...(expiresAt ? { expires_at: expiresAt } : {}),
      });
    historyResponse.data = {
      ...historyResponse.data,
      pending_credential_requests: [registrationCard({})],
    };
    const historyReads = () =>
      apiGet.mock.calls.filter(
        ([path]) => path === "/workflow/copilot/chat-history",
      ).length;
    vi.useFakeTimers();
    const readsBefore = historyReads();
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Generate and save" }),
      );
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(historyReads()).toBeGreaterThan(readsBefore);
    expect(screen.queryByText(/The vault didn't confirm the save/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Generate and save" }),
    ).toBeNull();

    const extendedDeadline = new Date(Date.now() + 600_000).toISOString();
    historyResponse.data = {
      ...historyResponse.data,
      pending_credential_requests: [
        registrationCard({ outcome: "unknown" }, extendedDeadline),
      ],
    };
    await act(async () => vi.advanceTimersByTimeAsync(2_000));

    expect(
      screen.getAllByText(/The vault didn't confirm the save/).length,
    ).toBeGreaterThan(0);
    await act(async () => vi.advanceTimersByTimeAsync(300_001));
    expect(
      screen.getAllByText(/The vault didn't confirm the save/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("Timed out")).toBeNull();
    expect(credentialResponsePosts()).toHaveLength(0);
    expect(toastFn).not.toHaveBeenCalled();
  });

  it("connect with an existing matched credential POSTs the credential_id", async () => {
    credentialsData.current = [
      {
        credential_id: "cred-hn",
        name: "HN Login",
        tested_url: "https://news.ycombinator.com/login",
      },
    ];
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    const useButton = await screen.findByRole("button", { name: "HN Login" });
    await act(async () => {
      fireEvent.click(useButton);
    });
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    const pickBody = credentialResponsePosts()[0]![1] as Record<
      string,
      unknown
    >;
    expect(pickBody).toMatchObject({
      turn_id: "turn-1",
      workflow_copilot_chat_id: "chat-1",
      resume_token: "rt-abc",
      action: "connected",
      credential_id: "cred-hn",
    });
    expect(Object.keys(pickBody).sort()).toEqual([
      "action",
      "credential_id",
      "resume_token",
      "turn_id",
      "workflow_copilot_chat_id",
    ]);
    expect(postStreaming).toHaveBeenCalledTimes(1);
    // Receipt keeps the credential name after the turn goes terminal.
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-1",
        message: "Connected. Continuing.",
        updated_workflow: null,
        response_time: "2026-07-13T00:00:06Z",
        proposal_disposition: "no_proposal",
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          responseType: "REPLY",
          terminal: "response",
          terminalMessage: "Connected. Continuing.",
          startedAt: "2026-07-13T00:00:00Z",
          endedAt: "2026-07-13T00:00:06Z",
          credentialPause: { outcome: "connected", credentialId: "cred-hn" },
        },
      });
      streamCalls[0]!.resolve();
    });
    expect(await screen.findByText(/Credential 'HN Login' added/)).toBeTruthy();
  });

  it("resolves a picked existing credential mid-turn on a brand-new chat", async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    credentialsData.current = [
      {
        credential_id: "cred-hn",
        name: "HN Login",
        tested_url: "https://news.ycombinator.com/login",
      },
    ];
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "HN Login" }));
    });
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    expect(await screen.findByText(/Credential 'HN Login' added/)).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it.each([
    ["credential_missing_totp", "Add 2FA method", "authenticator"],
    ["credential_rejected_by_site", "Update credential", undefined],
  ])(
    "a second %s card in the same turn stays actionable and answers with its own resume token",
    async (reason, buttonName, expectedTotpType) => {
      credentialsData.current = [
        {
          credential_id: "cred-hn",
          name: "HN Login",
          tested_url: "https://news.ycombinator.com/login",
        },
      ];
      await renderChat();
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(turnStart());
        streamCalls[0]!.onMessage(credentialFrame());
      });
      await act(async () => {
        fireEvent.click(await screen.findByRole("combobox"));
      });
      await act(async () => {
        fireEvent.click(
          await screen.findByRole("button", { name: "HN Login" }),
        );
      });
      await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));

      await act(async () => {
        streamCalls[0]!.onMessage(
          credentialFrame({
            resume_token: "rt-update",
            reason,
            credential_refs: ["cred-hn"],
          }),
        );
      });
      const update = await screen.findByRole("button", {
        name: buttonName,
      });
      await waitFor(() =>
        expect((update as HTMLButtonElement).disabled).toBe(false),
      );
      await act(async () => {
        fireEvent.click(update);
      });
      expect(modalEditingCredentialId.current).toBe("cred-hn");
      expect(modalDefaultTotpType.current).toBe(expectedTotpType);
      await act(async () => {
        fireEvent.click(await screen.findByTestId("mock-create-credential"));
      });
      await waitFor(() => expect(credentialResponsePosts()).toHaveLength(2));
      expect(credentialResponsePosts()[1]![1]).toMatchObject({
        resume_token: "rt-update",
        action: "connected",
      });
    },
  );

  it("connect CTA opens the modal, then a created credential POSTs connected", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Connect credential" }),
      );
    });
    const createBtn = await screen.findByTestId("mock-create-credential");
    // A sign-in pause forces the password credential form regardless of any
    // lingering ?type= param.
    expect(modalOverrideType.current).toBe("password");
    await act(async () => {
      fireEvent.click(createBtn);
    });
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      action: "connected",
      credential_id: "new-cred-1",
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("shows the full org credential list on a pause ask (not just the frame's candidates) and answers via the typed POST", async () => {
    credentialsData.current = [
      { credential_id: "cred-abc", name: "abc", tested_url: null },
      { credential_id: "cred-spare", name: "spare-portal", tested_url: null },
      { credential_id: "cred-other", name: "unrelated", tested_url: null },
    ];
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(
        credentialFrame({ credential_refs: ["cred-abc", "cred-spare"] }),
      );
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    // The copilot suggestion (credential_refs) is pinned under "Suggested"; the full org list —
    // including the credential NOT in credential_refs — stays below to override.
    expect(await screen.findByText("Suggested")).toBeTruthy();
    expect(screen.getByText("All credentials")).toBeTruthy();
    expect(screen.getByRole("button", { name: "abc" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "spare-portal" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "unrelated" })).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "spare-portal" }));
    });
    // The pick answers through the typed resume POST (which origin-binds), not a chat message.
    await waitFor(() => expect(credentialResponsePosts()).toHaveLength(1));
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      action: "connected",
      credential_id: "cred-spare",
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("sends one resume POST when a second pick races the first", async () => {
    let releaseFirstPost = () => {};
    sansApiPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseFirstPost = () => resolve({});
        }),
    );
    credentialsData.current = [
      { credential_id: "cred-first", name: "first", tested_url: null },
      { credential_id: "cred-second", name: "second", tested_url: null },
    ];
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "first" }));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "second" }));
    });
    expect(credentialResponsePosts()).toHaveLength(1);
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      credential_id: "cred-first",
    });
    await act(async () => {
      releaseFirstPost();
    });
    expect(credentialResponsePosts()).toHaveLength(1);
  });

  it("keeps the card actionable, toasts, and never logs the raw error (resume_token leak) when the resume POST fails", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    // AxiosError-shaped: config.data carries the one-time resume_token.
    sansApiPost.mockRejectedValueOnce(
      Object.assign(new Error("Request failed with status code 404"), {
        config: { data: JSON.stringify({ resume_token: "rt-secret" }) },
      }),
    );
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    });
    await waitFor(() => expect(toastFn).toHaveBeenCalled());
    // Still actionable for a retry.
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    const logged = errSpy.mock.calls.find((call) =>
      String(call[0]).includes("Failed to send credential response"),
    );
    expect(logged).toBeTruthy();
    expect(typeof logged![1]).toBe("string");
    expect(JSON.stringify(logged)).not.toContain("rt-secret");
    errSpy.mockRestore();
  });

  it("degrades a pause ask to the Connect-credential CTA when the credentials fetch fails", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    credsFail.current = true;
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-a"));
      streamCalls[0]!.onMessage(credentialFrame({ turn_id: "turn-a" }));
    });
    await waitFor(() =>
      expect(credentialsGets().length).toBeGreaterThanOrEqual(1),
    );
    // The notice sits in the tray, and a second copy of the ask in the transcript would add a button.
    const tray = await screen.findByRole("group", { name: "Sign-in request" });
    expect(
      within(tray).getAllByText(/Couldn't load your saved logins/),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("button", { name: "Connect credential" }),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByText("Saved logins")).toBeNull();
    errSpy.mockRestore();
  });

  it("renders no card when the pause resolved to declined (frame never shown)", async () => {
    await renderChat();
    await submit("use my saved login");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-7"));
      streamCalls[0]!.onMessage(terminalDeclinedResponse("turn-7"));
      streamCalls[0]!.resolve();
    });
    expect(screen.queryByText(/Copilot needs to sign in to/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
  });

  it("terminal-mode connect/skip never hits the network", async () => {
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    expect(
      screen.getByText(/Copilot needs to sign in to the site/),
    ).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    });
    expect(credentialResponsePosts()).toHaveLength(0);
  });

  it("clears the pause card on a terminal error — no dead actionable card", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    await act(async () => {
      streamCalls[0]!.onMessage(errorFrame());
      streamCalls[0]!.resolve();
    });
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Skip for now" })).toBeNull();
  });

  it("terminal connect via the modal auto-continues once and morphs the receipt", async () => {
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Connect credential" }),
      );
    });
    const createBtn = await screen.findByTestId("mock-create-credential");
    await act(async () => {
      fireEvent.click(createBtn);
    });
    // A fresh turn is sent (exactly one) referencing the connected credential by id (the
    // deterministic _explicit_credential_ids path), not by name.
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.message).toContain("new-cred-1");
    expect(streamCalls[1]!.body.message).toContain("continue");
    expect(credentialResponsePosts()).toHaveLength(0);
    expect(
      await screen.findByText("Continuing with 'New Login'…"),
    ).toBeTruthy();
  });

  it("fetches the org credentials on a terminal ask and offers the picker", async () => {
    credentialsData.current = [
      { credential_id: "cred_hn", name: "HN login", tested_url: null },
    ];
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    // The real card fetches /credentials and surfaces the searchable picker alongside the CTA.
    expect(await screen.findByText("Saved logins")).toBeTruthy();
    expect(apiGet).toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: { page: 1, page_size: 100, credential_type: "password" },
      }),
    );
  });

  it("does not receipt a terminal credential connect that the Accept fence blocked", async () => {
    credentialsData.current = [
      { credential_id: "cred_hn", name: "HN login", tested_url: null },
    ];
    // A terminal credential ask recovered from history while the server still reports a live
    // Accept claim, so the fence is already up when the card renders. Seeding from history is
    // what makes this reachable: the fence disables the composer a turn would need.
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    // The claim has to be one the row can tie to a proposal, or it is not OUR Accept and the
    // fence deliberately does not arm for it - a claim held by another writer holds Save, not
    // this chat's sending. Before the server reported claim liveness independently of the
    // proposal, a bare claim implied ours; it no longer does.
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-9",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Connect a credential to continue.",
        created_at: new Date().toISOString(),
        narrative_payload: terminalPromptResponse("turn-9").narrative_payload,
      },
    ];
    await renderChat();

    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "HN login" }));
    });

    // The fence blocked the continuation, so the card must not show a success-shaped receipt
    // for a turn that did not move. The picker stays actionable so the user can choose again
    // once the fence lifts, instead of holding a checkmark for a stopped step.
    expect(postStreaming).not.toHaveBeenCalled();
    expect(screen.queryByText(/Credential .*added/)).toBeNull();
    expect(screen.queryByText("Continuing with 'HN login'…")).toBeNull();
    expect(await screen.findByRole("combobox")).toBeTruthy();
  });

  it("restores the terminal picker when the auto-continue send fails", async () => {
    credentialsData.current = [
      { credential_id: "cred_hn", name: "HN login", tested_url: null },
    ];
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    // Pick an existing credential from the terminal picker → the auto-continue send fires.
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "HN login" }));
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Continuing with 'HN login'…")).toBeTruthy();
    // The continuation stream fails.
    await act(async () => {
      streamCalls[1]!.onMessage(turnStart("turn-10"));
      streamCalls[1]!.onMessage(errorFrame("turn-10"));
      streamCalls[1]!.resolve();
    });
    // The optimistic "connected" receipt is rolled back and the picker returns, even though the
    // error message is now the tail — so the user can re-pick instead of hitting a dead end.
    expect(await screen.findByRole("combobox")).toBeTruthy();
    expect(screen.queryByText("Continuing with 'HN login'…")).toBeNull();
    // Re-picking from the restored picker actually resumes: a fresh continuation fires even though
    // the stranded ask is no longer the literal tail.
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "HN login" }));
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
  });

  it("restores the terminal picker when the auto-continue is cancelled", async () => {
    credentialsData.current = [
      { credential_id: "cred_hn", name: "HN login", tested_url: null },
    ];
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "HN login" }));
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Continuing with 'HN login'…")).toBeTruthy();
    // Cancelling the continuation (a cancelled terminal, not an error) must also roll the optimistic
    // receipt back so it doesn't strand a permanent "Continuing…" with no continuation behind it.
    await act(async () => {
      streamCalls[1]!.onMessage(turnStart("turn-10"));
      streamCalls[1]!.onMessage(cancelledContinuationResponse("turn-10"));
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("combobox")).toBeTruthy();
    expect(screen.queryByText("Continuing with 'HN login'…")).toBeNull();
    // A stranded ask is no longer the tail turn, so it stays inline instead of holding the dock.
    expect(screen.queryByRole("group", { name: "Sign-in request" })).toBeNull();
    expect(useCopilotHeaderStore.getState().attention).toBeNull();
  });

  it("hides a stale terminal ask once it is no longer the last message", async () => {
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    // A new message makes the credential ask non-tail; its actionable card must disappear — picking
    // on a stale card would only show a misleading receipt with no backend call.
    await submit("actually, do something else");
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Connect credential" }),
      ).toBeNull(),
    );
  });

  it("does not auto-continue while a turn is still in flight", async () => {
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    // Emit the terminal card but never resolve the stream — isLoading stays
    // true, so the connect must not fire a fresh turn.
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Connect credential" }),
      );
    });
    const createBtn = await screen.findByTestId("mock-create-credential");
    await act(async () => {
      fireEvent.click(createBtn);
    });
    expect(
      await screen.findByText("Credential 'New Login' added"),
    ).toBeTruthy();
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("passes the pause frame's login URL to the modal as defaultTestUrl", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Connect credential" }),
      );
    });
    await screen.findByTestId("mock-create-credential");
    expect(modalDefaultTestUrl.current).toBe(
      "https://news.ycombinator.com/login",
    );
  });

  it("passes no defaultTestUrl for a terminal card (frame carries no URL)", async () => {
    await renderChat();
    await submit("who am I signing in as?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalPromptResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Connect credential" }),
      );
    });
    await screen.findByTestId("mock-create-credential");
    expect(modalDefaultTestUrl.current).toBeUndefined();
  });

  it("renders the auto-bind receipt from the credentialAutoBound signal", async () => {
    await renderChat();
    await submit("sign in and grab my dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalAutoBoundResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByText("Using credential 'Work login'"),
    ).toBeTruthy();
  });

  it("Change re-picks through the existing terminal-continue path (no third path)", async () => {
    credentialsData.current = [
      { credential_id: "cred_work", name: "Work login", tested_url: null },
      {
        credential_id: "cred_personal",
        name: "Personal login",
        tested_url: null,
      },
    ];
    await renderChat();
    await submit("sign in and grab my dashboard");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage(terminalAutoBoundResponse("turn-9"));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByText("Using credential 'Work login'"),
    ).toBeTruthy();
    // Open the Change picker and pick a different credential.
    await act(async () => {
      fireEvent.click(await screen.findByRole("combobox"));
    });
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Personal login" }),
      );
    });
    // A fresh turn fires referencing the picked credential by id (the deterministic continue path),
    // and NOT a typed credential-response POST — the same path a terminal ask pick uses.
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.message).toContain("cred_personal");
    expect(streamCalls[1]!.body.message).toContain("continue");
    expect(credentialResponsePosts()).toHaveLength(0);
    expect(
      await screen.findByText("Continuing with 'Personal login'…"),
    ).toBeTruthy();
  });

  it("defers the auto-bind receipt to a co-occurring credential ask", async () => {
    await renderChat();
    await submit("sign in to both sites");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const base = terminalAutoBoundResponse("turn-9");
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart("turn-9"));
      streamCalls[0]!.onMessage({
        ...base,
        narrative_payload: {
          ...base.narrative_payload,
          credentialPrompt: { reason: "credential_name_unresolved" },
        },
      });
      streamCalls[0]!.resolve();
    });
    // The credential ask owns this turn's credential UI and its credentialResolutions entry; the
    // auto-bind receipt defers so it can't adopt the ask's resolution.
    expect(screen.queryByText(/Using credential/)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
  });
});

describe("WorkflowCopilotChat — auto-bound receipt chronology", () => {
  const autoBoundResponse = (
    turnId: string,
    turnIndex: number,
    message: string,
  ) => {
    const response = terminalAutoBoundResponse(turnId);
    return {
      ...response,
      message,
      narrative_payload: {
        ...response.narrative_payload,
        turnIndex,
        terminalMessage: message,
        narrativeSummary: message,
        credentialAutoBound: {
          credentialId: "cred_acme_test",
          name: "acme-test",
        },
      },
    };
  };

  const historyMessage = (
    turnId: string,
    turnIndex: number,
    message: string,
  ) => {
    const response = autoBoundResponse(turnId, turnIndex, message);
    return {
      sender: "ai" as const,
      content: message,
      created_at: response.response_time,
      narrative_payload: response.narrative_payload,
      turn_outcome: null,
    };
  };

  async function completeAutoBoundTurn(
    callIndex: number,
    turnId: string,
    message: string,
  ) {
    await submit(`prompt ${callIndex + 1}`);
    await waitFor(() =>
      expect(postStreaming).toHaveBeenCalledTimes(callIndex + 1),
    );
    await act(async () => {
      streamCalls[callIndex]!.onMessage(turnStart(turnId));
      streamCalls[callIndex]!.onMessage(
        autoBoundResponse(turnId, callIndex, message),
      );
      streamCalls[callIndex]!.resolve();
    });
  }

  it("keeps a repeated live receipt on the first turn", async () => {
    await renderChat();
    await completeAutoBoundTurn(0, "turn-chronology-1", "First turn complete.");
    await completeAutoBoundTurn(
      1,
      "turn-chronology-2",
      "Second turn complete.",
    );

    const receipts = screen.getAllByText("Using credential 'acme-test'");
    expect(receipts).toHaveLength(1);
    expect(
      screen
        .getByText("First turn complete.")
        .closest('[role="status"]')
        ?.contains(receipts[0]!),
    ).toBe(true);
    expect(
      screen
        .getByText("Second turn complete.")
        .closest('[role="status"]')
        ?.contains(receipts[0]!),
    ).toBe(false);
  });

  it("keeps the surviving scrollback receipt read-only", async () => {
    await renderChat();
    await completeAutoBoundTurn(0, "turn-readonly-1", "First turn complete.");
    expect(await screen.findByRole("button", { name: "Change" })).toBeTruthy();

    await completeAutoBoundTurn(1, "turn-readonly-2", "Second turn complete.");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Change" })).toBeNull(),
    );
  });

  it("hydrates one receipt at the first persisted position", async () => {
    historyResponse.data.chat_history = [
      historyMessage("turn-history-1", 0, "First persisted turn."),
      historyMessage("turn-history-2", 1, "Second persisted turn."),
    ];

    await renderChat();
    await waitFor(() =>
      expect(
        screen.queryAllByText("Using credential 'acme-test'"),
      ).toHaveLength(1),
    );

    const receipt = screen.getByText("Using credential 'acme-test'");
    expect(
      screen
        .getByText("First persisted turn.")
        .closest('[role="status"]')
        ?.contains(receipt),
    ).toBe(true);
    expect(
      screen
        .getByText("Second persisted turn.")
        .closest('[role="status"]')
        ?.contains(receipt),
    ).toBe(false);
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    expect(credentialsGets()).toHaveLength(0);
  });
});
