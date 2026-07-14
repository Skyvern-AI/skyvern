import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { COPILOT_UX_V1_FLAG } from "@/util/featureFlags";

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
  toastFn,
  flagMap,
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
    toastFn: vi.fn(),
    flagMap: { current: {} as Record<string, boolean> },
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

// The real modal pulls react-query; a stub is enough to prove the connect →
// modal → onCredentialCreated wiring.
vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: ({
    isOpen,
    onCredentialCreated,
    overrideType,
  }: {
    isOpen?: boolean;
    onCredentialCreated?: (id: string) => void;
    overrideType?: string;
  }) => {
    modalOverrideType.current = overrideType;
    return isOpen ? (
      <button
        type="button"
        data-testid="mock-create-credential"
        onClick={() => onCredentialCreated?.("new-cred-1")}
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
  };
});

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => flagMap.current[flag] ?? false,
}));

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
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/Message Skyvern Copilot/)).toBeTruthy(),
  );
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
  flagMap.current = {};
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

describe("WorkflowCopilotChat — credential card wiring (flag on)", () => {
  it("sends supports_credential_pause on the request", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]!.body.supports_credential_pause).toBe(true);
  });

  it("renders the inline-pause card when a credential_required frame arrives", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    const useButton = await screen.findByRole("button", {
      name: /Use 'HN Login'/,
    });
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
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
  });

  it("keeps the card actionable, toasts, and never logs the raw error (resume_token leak) when the resume POST fails", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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

  it("retries the credentials fetch on a later pause after a transient failure (no poisoned cache)", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    const afterFailure = credentialsGets().length;
    // A later pause frame must trigger a fresh fetch — caching [] on failure
    // would have left credentialsList non-null and blocked all retries.
    credsFail.current = false;
    await act(async () => {
      streamCalls[0]!.onMessage(credentialFrame({ turn_id: "turn-b" }));
    });
    await waitFor(() =>
      expect(credentialsGets().length).toBeGreaterThan(afterFailure),
    );
    errSpy.mockRestore();
  });

  it("renders no card when the pause resolved to declined (frame never shown)", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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
});

describe("WorkflowCopilotChat — credential card (flag off parity)", () => {
  it("omits supports_credential_pause and renders no card even if a frame arrives", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]!.body.supports_credential_pause).toBeUndefined();
    await act(async () => {
      streamCalls[0]!.onMessage(turnStart());
      streamCalls[0]!.onMessage(credentialFrame());
    });
    expect(
      screen.queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
  });
});
