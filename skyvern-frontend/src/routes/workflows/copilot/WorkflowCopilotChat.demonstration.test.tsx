import type { ComponentProps } from "react";
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

import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import {
  canonicalRecoveriesByWorkflow,
  WorkflowCopilotChat,
} from "./WorkflowCopilotChat";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", MockResizeObserver);

type StreamCall = {
  path: string;
  body: Record<string, unknown>;
  onMessage: (payload: Record<string, unknown>) => boolean;
};

const {
  streamCalls,
  postStreaming,
  sansApiPost,
  getClientMock,
  activeDemonstration,
  credentials,
  historyResponse,
  sansApiGet,
  demonstrationFlag,
} = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const active = { value: null as Record<string, unknown> | null };
  const savedCredentials = {
    value: [
      {
        credential_id: "credential-1",
        credential_type: "password",
        name: "Saved login",
        credential: {
          username: "user@example.com",
          totp_type: "authenticator",
        },
      },
    ],
  };
  const history = {
    data: {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: null,
      chat_history: [] as Record<string, unknown>[],
      proposed_workflow: null,
      proposed_workflow_metadata: null,
      auto_accept: false,
      pending_credential_requests: [],
      question_interactions: [],
      pending_question_cancel_token: null,
    },
  };
  const post = vi.fn().mockResolvedValue({ data: { seq: 1 } });
  const get = vi.fn((path: string) => {
    if (path === "/workflow/copilot/demonstration/active") {
      return Promise.resolve({ data: active.value });
    }
    if (path === "/credentials") {
      return Promise.resolve({ data: savedCredentials.value });
    }
    return Promise.resolve(history);
  });
  return {
    streamCalls: calls,
    postStreaming: vi.fn(
      (
        path: string,
        body: Record<string, unknown>,
        onMessage: (payload: Record<string, unknown>) => boolean,
      ) => {
        calls.push({ path, body, onMessage });
        if (path.endsWith("/chat-post")) return Promise.resolve();
        return new Promise<void>(() => {});
      },
    ),
    sansApiPost: post,
    getClientMock: vi.fn().mockResolvedValue({ get, post }),
    activeDemonstration: active,
    credentials: savedCredentials,
    historyResponse: history,
    sansApiGet: get,
    demonstrationFlag: { value: true },
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));
vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => demonstrationFlag.value,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => ({ workflowPermanentId: "wpid_1" }),
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
  title: "Test workflow",
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
        setSaveBlockedReason: vi.fn(),
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
  render(
    <QueryClientProvider client={queryClient}>
      <WorkflowCopilotChat docked={false} {...props} />
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("group", { name: "Copilot message composer" }),
    ).toBeTruthy(),
  );
}

async function startDemonstration({
  canFillCredentials = true,
}: { canFillCredentials?: boolean } = {}) {
  const previousRecordCalls = streamCalls.filter((call) =>
    call.path.endsWith("/record"),
  ).length;
  await waitFor(() =>
    expect(
      useRecordingLauncherStore.getState().startDemonstration,
    ).toBeTruthy(),
  );
  act(() => {
    useRecordingLauncherStore.getState().startDemonstration?.({
      insertion: { after_label: "open_form", before_label: "finish" },
    });
  });
  await waitFor(() =>
    expect(
      streamCalls.filter((call) => call.path.endsWith("/record")),
    ).toHaveLength(previousRecordCalls + 1),
  );
  const call = [...streamCalls]
    .reverse()
    .find((item) => item.path.endsWith("/record"));
  if (!call) throw new Error("Recorder stream did not open");
  act(() => {
    call.onMessage({
      type: "demonstration_started",
      demonstration_id: "demonstration-1",
      browser_session_id: "browser-1",
      action_count: 0,
      resumed: false,
      can_fill_credentials: canFillCredentials,
    });
  });
  return call;
}

beforeEach(() => {
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useRecordingLauncherStore.setState(
    useRecordingLauncherStore.getInitialState(),
  );
  sessionStorage.clear();
  streamCalls.length = 0;
  postStreaming.mockClear();
  sansApiPost.mockClear();
  sansApiPost.mockResolvedValue({ data: { seq: 1 } });
  activeDemonstration.value = null;
  demonstrationFlag.value = true;
  sansApiGet.mockClear();
  credentials.value = [
    {
      credential_id: "credential-1",
      credential_type: "password",
      name: "Saved login",
      credential: { username: "user@example.com", totp_type: "authenticator" },
    },
  ];
  historyResponse.data.request_turn_id = null;
  historyResponse.data.chat_history = [];
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
});

afterEach(() => {
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

describe("WorkflowCopilotChat demonstration", () => {
  it("never looks for an active demonstration when the feature is off", async () => {
    demonstrationFlag.value = false;
    await renderChat({ liveBrowserSessionId: "browser-1" });
    await waitFor(() => expect(sansApiGet).toHaveBeenCalled());
    expect(
      sansApiGet.mock.calls.some(
        ([path]) => path === "/workflow/copilot/demonstration/active",
      ),
    ).toBe(false);
  });

  it("opens the recorder stream and renders recorded actions in the transcript", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    const call = await startDemonstration();
    await waitFor(() =>
      expect(
        useRecordingLauncherStore.getState()
          .demonstrationRecordingBrowserSessionId,
      ).toBe("browser-1"),
    );

    expect(call.body).toEqual({
      workflow_permanent_id: "wpid_1",
      browser_session_id: "browser-1",
      insertion: { after_label: "open_form", before_label: "finish" },
    });
    const status = screen
      .getByText("Recording · 0 actions")
      .closest('[role="status"]');
    expect(status?.querySelector(".bg-red-500")).toBeTruthy();
    expect(status?.querySelector("svg")).toBeNull();
    const doneButton = screen.getByRole("button", {
      name: "Done, build the workflow",
    });
    expect((doneButton as HTMLButtonElement).disabled).toBe(false);
    const attachButton = screen.getByRole("button", { name: "Attach a file" });
    expect((attachButton as HTMLButtonElement).disabled).toBe(true);
    expect(attachButton.className).toContain("disabled:opacity-50");
    expect(attachButton.className).not.toContain(
      "disabled:pointer-events-none",
    );
    act(() => {
      call.onMessage({
        type: "demonstration_action",
        seq: 1,
        label: "Open form",
        tool_name: "click",
      });
      call.onMessage({
        type: "demonstration_action",
        seq: 2,
        label: "Submit form",
        tool_name: "click",
      });
    });

    const group = await screen.findByRole("group", {
      name: "Your demonstration",
    });
    expect(
      within(group)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["1Open form", "2Submit form"]);
    expect(within(group).getByText(/2 steps/)).toBeTruthy();
    expect((doneButton as HTMLButtonElement).disabled).toBe(false);

    postStreaming.mockImplementationOnce((path, body, onMessage) => {
      streamCalls.push({ path, body, onMessage });
      queueMicrotask(() =>
        onMessage({
          type: "turn_start",
          turn_id: "turn-demonstration",
          turn_index: 0,
          mode: "build",
          timestamp: "2026-10-14T12:00:00Z",
        }),
      );
      return new Promise(() => {});
    });
    fireEvent.click(doneButton);
    await waitFor(() =>
      expect(streamCalls.some((item) => item.path.endsWith("/chat-post"))).toBe(
        true,
      ),
    );
    expect(screen.queryByText(/^Recording/)).toBeNull();
    expect(
      useRecordingLauncherStore.getState()
        .demonstrationRecordingBrowserSessionId,
    ).toBeNull();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(
      false,
    );
    expect(
      within(
        screen.getByRole("group", { name: "Your demonstration" }),
      ).queryByRole("listitem"),
    ).toBeNull();
  });

  it("waits for stop and preserves a failed demonstration send for retry", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    const call = await startDemonstration();
    act(() => {
      call.onMessage({
        type: "demonstration_action",
        seq: 1,
        label: "Open form",
        tool_name: "click",
      });
      call.onMessage({
        type: "demonstration_action",
        seq: 2,
        label: "Submit form",
        tool_name: "click",
      });
    });

    let resolveStop: ((value: { data: { seq: number } }) => void) | undefined;
    sansApiPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveStop = resolve;
        }),
    );
    postStreaming.mockImplementationOnce((path, body, onMessage) => {
      streamCalls.push({ path, body, onMessage });
      return Promise.reject(
        Object.assign(new Error("Recording is still active"), { status: 409 }),
      );
    });

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Done, build the workflow",
      }),
    );
    await waitFor(() =>
      expect(sansApiPost).toHaveBeenCalledWith(
        "/workflow/copilot/demonstration/control",
        { demonstration_id: "demonstration-1", action: "stop" },
      ),
    );
    expect(streamCalls.some((item) => item.path.endsWith("/chat-post"))).toBe(
      false,
    );
    act(() => {
      resolveStop?.({ data: { seq: 1 } });
    });

    await waitFor(() =>
      expect(
        streamCalls.find((item) => item.path.endsWith("/chat-post"))?.body,
      ).toMatchObject({
        message:
          "I've shown you the task in the browser. Build a workflow that does it.",
        product_action: "demonstrate",
        demonstration_id: "demonstration-1",
      }),
    );
    expect(
      streamCalls.find((item) => item.path.endsWith("/chat-post"))?.body,
    ).not.toHaveProperty("demonstration_insertion");

    expect(
      await screen.findByText("Recording paused · 2 actions"),
    ).toBeTruthy();
    expect(
      useRecordingLauncherStore.getState()
        .demonstrationRecordingBrowserSessionId,
    ).toBeNull();
    expect(
      screen.getByText("I'll show you the task in the browser."),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Attach a file" }).title).toBe(
      "Use Build before attaching a file",
    );
    expect(
      (
        screen.getByRole("button", {
          name: "Build the workflow",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);

    const group = await screen.findByRole("group", {
      name: "Your demonstration",
    });
    expect(
      within(group)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["1Open form", "2Submit form"]);

    fireEvent.click(screen.getByRole("button", { name: "Build the workflow" }));
    await waitFor(() =>
      expect(
        streamCalls.filter((item) => item.path.endsWith("/chat-post")),
      ).toHaveLength(2),
    );
    await waitFor(() =>
      expect(screen.queryByText("Recording paused · 2 actions")).toBeNull(),
    );

    const sentGroup = screen.getByRole("group", {
      name: "Your demonstration",
    });
    await waitFor(() =>
      expect(within(sentGroup).queryByRole("listitem")).toBeNull(),
    );
    fireEvent.click(within(sentGroup).getByRole("button", { expanded: false }));
    expect(within(sentGroup).getAllByRole("listitem")).toHaveLength(2);
  });

  it("stops a recording with nothing captured without building", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    await startDemonstration();
    sansApiPost.mockResolvedValueOnce({ data: { seq: 1, action_count: 0 } });

    fireEvent.click(
      screen.getByRole("button", { name: "Done, build the workflow" }),
    );

    expect(
      await screen.findByText("Do something in the browser first"),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Build the workflow",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(sansApiPost).toHaveBeenCalledWith(
      "/workflow/copilot/demonstration/control",
      { demonstration_id: "demonstration-1", action: "stop" },
    );
  });

  it("removes a failed start message, then discards a recording", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    let rejectStart: ((reason: Error) => void) | undefined;
    postStreaming.mockImplementationOnce((path, body, onMessage) => {
      streamCalls.push({ path, body, onMessage });
      return new Promise<void>((_resolve, reject) => {
        rejectStart = reject;
      });
    });
    act(() => {
      useRecordingLauncherStore.getState().startDemonstration?.();
    });
    expect(
      await screen.findByText("I'll show you the task in the browser."),
    ).toBeTruthy();
    act(() => rejectStart?.(new Error("Already recording elsewhere")));
    await waitFor(() =>
      expect(
        screen.queryByText("I'll show you the task in the browser."),
      ).toBeNull(),
    );
    streamCalls.length = 0;

    const call = await startDemonstration();
    act(() => {
      call.onMessage({
        type: "demonstration_action",
        seq: 1,
        label: "Open form",
        tool_name: "click",
      });
    });
    expect(
      await screen.findByRole("group", { name: "Your demonstration" }),
    ).toBeTruthy();

    sansApiPost.mockRejectedValueOnce(new Error("network down"));
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(sansApiPost).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Recording · 1 action")).toBeTruthy();
    expect(
      screen.getByRole("group", { name: "Your demonstration" }),
    ).toBeTruthy();

    sansApiPost.mockRejectedValueOnce(
      Object.assign(new Error("Demonstration has ended"), {
        isAxiosError: true,
        response: { status: 409 },
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));

    await waitFor(() =>
      expect(sansApiPost).toHaveBeenCalledWith(
        "/workflow/copilot/demonstration/control",
        { demonstration_id: "demonstration-1", action: "cancel" },
      ),
    );
    await waitFor(() => {
      expect(screen.queryByText("Recording · 1 action")).toBeNull();
      expect(
        screen.queryByRole("group", { name: "Your demonstration" }),
      ).toBeNull();
    });
  });

  it("keeps a completed demonstration when a later recording is discarded", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    const first = await startDemonstration();
    act(() => {
      first.onMessage({
        type: "demonstration_action",
        seq: 1,
        label: "Open first form",
        tool_name: "click",
      });
    });
    postStreaming.mockImplementationOnce((path, body, onMessage) => {
      streamCalls.push({ path, body, onMessage });
      onMessage({
        type: "turn_start",
        turn_id: "turn-first-demonstration",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-10-14T12:00:00Z",
      });
      onMessage({
        type: "response",
        message: "Built the first workflow.",
        workflow_copilot_chat_id: "chat-1",
        updated_workflow: null,
        response_time: "2026-10-14T12:00:02Z",
        proposal_disposition: "no_proposal",
        turn_id: "turn-first-demonstration",
      });
      return Promise.resolve();
    });
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Done, build the workflow",
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send" })).toBeTruthy(),
    );

    const second = await startDemonstration();
    act(() => {
      second.onMessage({
        type: "demonstration_action",
        seq: 1,
        label: "Open second form",
        tool_name: "click",
      });
    });
    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));

    await waitFor(() =>
      expect(screen.queryByText("Open second form")).toBeNull(),
    );
    const cards = screen.getAllByRole("group", {
      name: "Your demonstration",
    });
    expect(cards).toHaveLength(1);
    fireEvent.click(within(cards[0]!).getByRole("button", { expanded: false }));
    expect(within(cards[0]!).getByText("Open first form")).toBeTruthy();
  });

  it("shows the credential card, fills from a saved login, and dismisses on Skip", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    const call = await startDemonstration();
    act(() => {
      call.onMessage({
        type: "demonstration_sensitive_field",
        field: "password",
        selector: "#password",
        url: "https://example.com/login",
      });
    });

    expect(await screen.findByLabelText("Sign-in request")).toBeTruthy();
    fireEvent.click(await screen.findByText("Saved logins"));
    fireEvent.click(await screen.findByText("Saved login"));

    await waitFor(() =>
      expect(sansApiPost).toHaveBeenCalledWith(
        "/workflow/copilot/demonstration/control",
        {
          demonstration_id: "demonstration-1",
          action: "fill_credential",
          credential_id: "credential-1",
          selector: "#password",
          field: "password",
        },
      ),
    );

    act(() => {
      call.onMessage({
        type: "demonstration_action",
        seq: 2,
        label: 'Fill password from "Saved login"',
        tool_name: "fill_credential_field",
      });
      call.onMessage({
        type: "demonstration_sensitive_field",
        field: "password",
        selector: "#password-again",
        url: "https://example.com/login",
      });
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "Skip for now" }),
    );
    await waitFor(() =>
      expect(screen.queryByLabelText("Sign-in request")).toBeNull(),
    );

    act(() => {
      call.onMessage({
        type: "demonstration_sensitive_field",
        field: "other",
        selector: "#private-answer",
        url: "https://example.com/form",
      });
    });
    expect(
      await screen.findByText(
        "Sensitive field skipped — its value was not recorded.",
      ),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Sign-in request")).toBeNull();

    act(() => {
      call.onMessage({
        type: "demonstration_action",
        seq: 5,
        label: "Submit form",
        tool_name: "click",
      });
    });
    expect(
      screen.getByText("Sensitive field skipped — its value was not recorded."),
    ).toBeTruthy();
    const demonstration = screen.getByRole("group", {
      name: "Your demonstration",
    });
    expect(within(demonstration).getAllByRole("listitem")).toHaveLength(5);
    expect(within(demonstration).getByText(/Fill password from/)).toBeTruthy();
    expect(
      within(demonstration).getByText(/Fill sensitive field/),
    ).toBeTruthy();
  });

  it("keeps the type-it-yourself path when saved credential filling is unavailable", async () => {
    await renderChat({ liveBrowserSessionId: "browser-1" });
    const call = await startDemonstration({ canFillCredentials: false });
    act(() => {
      call.onMessage({
        type: "demonstration_sensitive_field",
        field: "password",
        selector: "#password",
        url: "https://example.com/login",
      });
    });

    const card = await screen.findByLabelText("Sign-in request");
    expect(within(card).queryByText("Saved logins")).toBeNull();
    expect(
      within(card).queryByRole("button", { name: "Connect credential" }),
    ).toBeNull();
    expect(
      within(card).getByRole("button", { name: "Skip for now" }),
    ).toBeTruthy();
  });

  it("hides Resume for a restored recording past its limit", async () => {
    activeDemonstration.value = {
      demonstration_id: "demonstration-recovered",
      status: "stopped",
      action_count: 1,
      action_labels: ["Open recovered form"],
      can_fill_credentials: false,
      can_resume: false,
    };

    await renderChat({ liveBrowserSessionId: "browser-1" });

    expect(
      await screen.findByRole("button", { name: "Build the workflow" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it.each(["stopped", "stopping"] as const)(
    "restores a %s recording and renders history demonstrations",
    async (status) => {
      activeDemonstration.value = {
        demonstration_id: "demonstration-recovered",
        status,
        action_count: 3,
        action_labels: [
          "Open recovered form",
          "Fill name",
          "Submit recovered form",
        ],
        can_fill_credentials: false,
        can_resume: true,
        insertion: { after_label: "first", before_label: "last" },
      };
      historyResponse.data.chat_history = [
        {
          sender: "user",
          content:
            "I've shown you the task in the browser. Build a workflow that does it.",
          created_at: "2026-10-14T12:00:00Z",
        },
        {
          sender: "ai",
          content: "Built the workflow.",
          narrative_payload: {
            demonstrationItems: ["Open form", "Submit form"],
          },
          created_at: "2026-10-14T12:01:00Z",
        },
      ];

      await renderChat({ liveBrowserSessionId: "browser-1" });

      if (status === "stopping") {
        postStreaming.mockImplementationOnce((path, body, onMessage) => {
          streamCalls.push({ path, body, onMessage });
          return Promise.reject(
            Object.assign(new Error("Recording is still active"), {
              status: 409,
            }),
          );
        });
      }

      const pausedText = await screen.findByText(
        "Recording paused · 3 actions",
      );
      const pausedStatus = pausedText.closest('[role="status"]');
      expect(pausedStatus).toBeTruthy();
      expect(pausedStatus?.querySelector("svg")).toBeTruthy();
      expect(pausedStatus?.querySelector(".bg-red-500")).toBeNull();
      expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Build the workflow" }),
      ).toBeTruthy();
      expect(screen.getByRole("button", { name: "Discard" })).toBeTruthy();

      const [historyCard, recoveredCard] = screen.getAllByRole("group", {
        name: "Your demonstration",
      });
      expect(recoveredCard).toBeTruthy();
      expect(
        within(recoveredCard!)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual([
        "1Open recovered form",
        "2Fill name",
        "3Submit recovered form",
      ]);
      expect(within(historyCard!).queryByRole("listitem")).toBeNull();
      fireEvent.click(
        within(historyCard!).getByRole("button", { expanded: false }),
      );
      expect(
        within(historyCard!)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual(["1Open form", "2Submit form"]);

      fireEvent.click(
        screen.getByRole("button", { name: "Build the workflow" }),
      );
      await waitFor(() =>
        expect(
          streamCalls.find((item) => item.path.endsWith("/chat-post"))?.body,
        ).toMatchObject({
          demonstration_id: "demonstration-recovered",
        }),
      );
      if (status === "stopping") {
        expect(
          await screen.findByText("Recording paused · 3 actions"),
        ).toBeTruthy();
        await waitFor(() =>
          expect(
            (
              screen.getByRole("button", {
                name: "Build the workflow",
              }) as HTMLButtonElement
            ).disabled,
          ).toBe(false),
        );
        fireEvent.click(
          screen.getByRole("button", { name: "Build the workflow" }),
        );
        await waitFor(() =>
          expect(
            streamCalls.filter((item) => item.path.endsWith("/chat-post")),
          ).toHaveLength(2),
        );
        await waitFor(() =>
          expect(screen.queryByText("Recording paused · 3 actions")).toBeNull(),
        );
      }
      expect(
        [...streamCalls]
          .reverse()
          .find((item) => item.path.endsWith("/chat-post"))?.body,
      ).not.toHaveProperty("demonstration_insertion");
    },
  );
});
