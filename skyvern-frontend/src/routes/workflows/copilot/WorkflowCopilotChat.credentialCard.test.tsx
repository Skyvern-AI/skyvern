import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowCopilotCredentialRequiredUpdate } from "./workflowCopilotTypes";

type StreamBody = {
  message: string;
  supports_credential_pause?: boolean;
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
      chat_history: [] as unknown[],
      proposed_workflow: null as Record<string, unknown> | null,
      auto_accept: false,
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
  }: {
    isOpen?: boolean;
    onCredentialCreated?: (id: string, name?: string) => void;
    overrideType?: string;
    defaultTestUrl?: string;
  }) => {
    modalOverrideType.current = overrideType;
    modalDefaultTestUrl.current = defaultTestUrl;
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
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat() {
  const view = render(<WorkflowCopilotChat docked={false} />);
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
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  apiPost.mockClear();
  apiPost.mockResolvedValue({});
  sansApiPost.mockClear();
  sansApiPost.mockResolvedValue({});
  apiGet.mockClear();
  toastFn.mockClear();
  credentialsData.current = [];
  credsFail.current = false;
  modalOverrideType.current = undefined;
  modalDefaultTestUrl.current = undefined;
  historyResponse.data = {
    workflow_copilot_chat_id: "chat-1",
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  cleanup();
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
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();
    expect(screen.queryByText("Explore site")).toBeNull();
  });
});

describe("WorkflowCopilotChat — credential card wiring", () => {
  it("sends supports_credential_pause on the request", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]!.body.supports_credential_pause).toBe(true);
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
      screen.getByText(/Copilot needs to sign in to news\.ycombinator\.com/),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
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
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      turn_id: "turn-1",
      workflow_copilot_chat_id: "chat-1",
      resume_token: "rt-abc",
      action: "skip",
    });
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
    expect(credentialResponsePosts()[0]![1]).toMatchObject({
      action: "connected",
      credential_id: "cred-hn",
    });
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
    // Failed fetch → no picker (null, not a cached []); the Connect-credential CTA remains so the
    // user can still create one, and the pause stays answerable.
    expect(
      await screen.findByRole("button", { name: "Connect credential" }),
    ).toBeTruthy();
    expect(screen.queryByText("Use existing…")).toBeNull();
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
    expect(await screen.findByText("Use existing…")).toBeTruthy();
    expect(apiGet).toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: { page: 1, page_size: 100, credential_type: "password" },
      }),
    );
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
