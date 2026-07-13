import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeatureFlagContext,
  FeatureFlagValueContext,
} from "@/hooks/useFeatureFlag";

type StreamBody = {
  message: string;
  mode: string | null;
  code_block: boolean | null;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { mockCopilotUxV1Enabled } = vi.hoisted(() => ({
  mockCopilotUxV1Enabled: vi.fn(() => true),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockCopilotUxV1Enabled(),
}));

const { streamCalls, postStreaming, cancelPost, historyResponse } = vi.hoisted(
  () => {
    const calls: StreamCall[] = [];
    const post = vi.fn().mockResolvedValue({});
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
        workflow_copilot_chat_id: null as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null as Record<string, unknown> | null,
        auto_accept: false,
      },
    };
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyResponse: history,
    };
  },
);

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation(() => Promise.resolve(historyResponse)),
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

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

type FlagConfig = {
  copilotV2?: boolean;
  codeBlockMode?: boolean;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
};

async function renderChat(flags: FlagConfig) {
  const booleanFlags: Record<string, boolean> = {
    ENABLE_WORKFLOW_COPILOT_V2: flags.copilotV2 ?? false,
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: flags.codeBlockMode ?? false,
    CODE_BLOCK_ACCESS: flags.codeBlockMode ?? false,
  };
  const view = render(
    <FeatureFlagContext.Provider value={(name) => booleanFlags[name]}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat
          requiresLiveBrowser={flags.requiresLiveBrowser}
          isLiveBrowserReady={flags.isLiveBrowserReady}
        />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>,
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

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  mockCopilotUxV1Enabled.mockReset();
  mockCopilotUxV1Enabled.mockReturnValue(true);
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — S4 composer, copilot_ux_v1 on", () => {
  it("defaults straight to Build with code when code-first is accessible", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(true);
    expect(
      screen.getByRole("button", { name: "Switch mode" }).textContent,
    ).toContain("Build with code");
  });

  it("falls back to plain Build when the code-block flag is off", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: false });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(null);
    const pillText = screen.getByRole("button", {
      name: "Switch mode",
    }).textContent;
    expect(pillText).toContain("Build");
    expect(pillText).not.toContain("Build with code");
  });

  it("opens the mode pill as a real Radix menu, not a hand-rolled div", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: true });
    const trigger = screen.getByRole("button", { name: "Switch mode" });
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");

    await act(async () => {
      fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    });
    expect(await screen.findByRole("menu")).toBeTruthy();

    const askItem = await screen.findByRole("menuitem", { name: "Ask" });
    await act(async () => {
      fireEvent.click(askItem);
    });
    await submit("what does this workflow do?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.mode).toBe("ask");
  });

  it("morphs to stop while running with an empty box, and cancels the run on click", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const button = screen.getByRole("button", { name: "Stop" });
    await act(async () => {
      fireEvent.click(button);
    });
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
    );
  });

  it("flips back to a queueing send when typing mid-run, and queues on click", async () => {
    await renderChat({ copilotV2: true, codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea(), {
      target: { value: "also grab the story scores" },
    });
    const button = screen.getByRole("button", {
      name: "Queue for next turn",
    });

    await act(async () => {
      fireEvent.click(button);
    });

    // Queued, not sent as a second concurrent turn.
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Queued")).toBeTruthy();
    // Exactly one status line — the Queued chip, not also the legacy
    // aria-live text line (browserStatusText is suppressed under S4 so the
    // two don't announce the same status twice).
    expect(
      screen.getAllByText("Queued — sends when this turn finishes."),
    ).toHaveLength(1);
  });

  it("disables the morph button (not a dead-looking Send) while a prompt waits on the live browser", async () => {
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      requiresLiveBrowser: true,
      isLiveBrowserReady: false,
    });
    await submit("build me a workflow");

    // No turn started yet — queued purely on the live-browser gate. The
    // status now lives on the queued bubble's footer, not the composer
    // chip (that's reserved for the working-reason queue) — getByText
    // throws on a duplicate match, so this also proves there's only one.
    expect(postStreaming).not.toHaveBeenCalled();
    expect(
      screen.getByText("Prompt queued. Waiting for live browser..."),
    ).toBeTruthy();
    expect(screen.queryByText("Queued")).toBeNull();

    const button = screen.getByRole("button", {
      name: "Send disabled — waiting for live browser",
    });
    expect((button as HTMLButtonElement).disabled).toBe(true);

    const cancel = screen.getByRole("button", {
      name: "Cancel queued message",
    });
    await act(async () => {
      fireEvent.click(cancel);
    });
    expect(screen.queryByText("Queued")).toBeNull();
    expect(
      screen.queryByText("Prompt queued. Waiting for live browser..."),
    ).toBeNull();
  });
});

describe("WorkflowCopilotChat — S4 composer, copilot_ux_v1 off (parity)", () => {
  it("keeps the legacy plain status line for a live-browser queue, with no bubble footer or chip", async () => {
    mockCopilotUxV1Enabled.mockReturnValue(false);
    await renderChat({
      copilotV2: true,
      codeBlockMode: true,
      requiresLiveBrowser: true,
      isLiveBrowserReady: false,
    });
    await submit("build me a workflow");

    expect(postStreaming).not.toHaveBeenCalled();
    // Same wording, but via the legacy aria-live status line — the S4
    // chip and the new bubble footer are both flag-gated on uxV1.
    expect(
      screen.getByText("Prompt queued. Waiting for live browser..."),
    ).toBeTruthy();
    expect(screen.queryByText("Queued")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Cancel queued message" }),
    ).toBeNull();
  });
});
