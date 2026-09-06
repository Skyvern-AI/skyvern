import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getSseClient } from "@/api/sse";
import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";

import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

// Capture every postStreaming call so a test can assert how many streams
// started and drive each one to a terminal frame on demand.
type StreamCall = {
  body: { message: string };
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};
const { streamCalls, postStreaming, cancelPost, historyResponse, speechState } =
  vi.hoisted(() => {
    const calls: StreamCall[] = [];
    const post = vi.fn().mockResolvedValue({});
    const streaming = vi.fn(
      (
        _path: string,
        body: { message: string },
        onMessage: (payload: unknown) => boolean,
      ) =>
        new Promise<void>((resolve, reject) => {
          calls.push({ body, onMessage, resolve, reject });
        }),
    );
    const history = {
      data: {
        workflow_copilot_chat_id: null as string | null,
        chat_history: [] as {
          sender: "user" | "ai";
          content: string;
          created_at: string;
          narrative_payload?: Record<string, unknown> | null;
        }[],
        proposed_workflow: null as Record<string, unknown> | null,
        auto_accept: false,
      },
    };
    const speech = {
      isSupported: false,
      isListening: false,
      isHearingSpeech: false,
      start: vi.fn(),
      stop: vi.fn<() => Promise<Blob | null>>().mockResolvedValue(null),
      toggle: vi.fn(),
      takeAudioBlob: vi.fn<() => Blob | null>().mockReturnValue(null),
    };
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyResponse: history,
      speechState: speech,
    };
  });

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

vi.mock("@/hooks/useSpeechToTextField", () => ({
  useSpeechToTextField: () => speechState,
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

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const terminalResponse = (
  message: string,
): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message,
  updated_workflow: null,
  response_time: "2026-05-25T00:00:05Z",
  proposal_disposition: "no_proposal",
});

const turnStart = () => ({
  type: "turn_start" as const,
  turn_id: "turn-1",
  turn_index: 0,
  mode: "build",
  timestamp: "2026-05-25T00:00:00Z",
});

const workflowDraft = () => ({
  type: "workflow_draft" as const,
  block_count: 2,
  block_labels: ["open_page", "add_to_cart"],
  summary: "two block workflow",
  timestamp: "2026-05-25T00:00:03Z",
  workflow: { workflow_id: "wf_draft" },
});

async function renderChat(
  props: ComponentProps<typeof WorkflowCopilotChat> = {},
) {
  const view = render(<WorkflowCopilotChat {...props} />);
  // Let the mount-time chat-history fetch settle.
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

async function renderChatWithFlags(booleanFlags: Record<string, boolean>) {
  const view = render(
    <FeatureFlagContext.Provider value={(name) => booleanFlags[name]}>
      <WorkflowCopilotChat />
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

// Code-block mode is off in the bare harness (no flag provider), so the turns
// it drives all explicitly select non-code Build; this one opts into the code composer.
function renderChatWithCodeMode() {
  return renderChatWithFlags({
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: true,
    CODE_BLOCK_ACCESS: true,
  });
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

// Deliver the first SSE frame of the newest pending stream, which is what arms
// the stop control.
async function deliverFirstFrame() {
  const call = streamCalls[streamCalls.length - 1];
  if (!call) throw new Error("no pending stream to open");
  await act(async () => {
    call.onMessage(turnStart());
  });
}

// Drive the oldest pending stream to a clean terminal frame.
async function completeOldestStream(message: string) {
  const call = streamCalls.find((c) => c.body.message !== undefined);
  if (!call) throw new Error("no pending stream to complete");
  await act(async () => {
    call.onMessage(terminalResponse(message));
    call.resolve();
  });
}

beforeEach(() => {
  // jsdom has no layout engine.
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  speechState.isSupported = false;
  speechState.isListening = false;
  speechState.isHearingSpeech = false;
  speechState.start.mockClear();
  speechState.stop.mockClear();
  speechState.stop.mockResolvedValue(null);
  speechState.toggle.mockClear();
  speechState.takeAudioBlob.mockClear();
  speechState.takeAudioBlob.mockReturnValue(null);
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
  useCopilotActionStore.setState({
    pendingBuild: null,
    generatingBlockLabel: null,
    cancelNonce: 0,
  });
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — keep the chat live during a turn", () => {
  it("reserves inline space for a user-message timestamp", async () => {
    const content = "How would I loop the same block over a list of websites?";
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "user",
          content,
          created_at: "2026-05-25T00:00:00Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await renderChat();

    const message = screen.getByText(content);
    const row = message.parentElement!;
    const timestamp = row.querySelector("span")!;
    expect(message.className).toContain("min-w-0");
    expect(message.className).toContain("flex-1");
    expect(row.className).toContain("items-end");
    expect(timestamp.className).toContain("shrink-0");
    expect(timestamp.className).not.toContain("absolute");
  });

  it("wraps long unbroken text inside the user-message bubble", async () => {
    const content = `https://example.test/logs?query=${"encoded-query-segment".repeat(20)}`;
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "user",
          content,
          created_at: "2026-05-25T00:00:00Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await renderChat();

    const message = screen.getByText(content);
    expect(message.className).toContain("[overflow-wrap:anywhere]");
  });

  it("leaves the input enabled while a turn is in flight", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    expect(textarea().disabled).toBe(false);
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  });

  it("labels the in-flight follow-up action as the next send", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    expect(screen.getByTestId("copilot-working-status")).toBeTruthy();
    expect(
      screen.getByPlaceholderText("Type to queue a message…"),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Queue" })).toBeNull();
  });

  it("explains that the next send waits while the live browser is starting", async () => {
    render(
      <WorkflowCopilotChat requiresLiveBrowser isLiveBrowserReady={false} />,
    );
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText("Type a prompt to send when ready..."),
      ).toBeTruthy(),
    );

    expect(
      screen.getByText(
        "Live browser is starting. Your next send will wait until it connects.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    // The old copy conflated sending with queuing; guard against its return.
    expect(screen.queryByRole("button", { name: "Queue" })).toBeNull();
    expect(screen.queryByText(/Send now to queue your prompt/)).toBeNull();
    expect(screen.queryByPlaceholderText(/Type a prompt to queue/)).toBeNull();
  });

  it("still sends the message when dictation audio upload fails", async () => {
    await renderChat();
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );
    cancelPost.mockRejectedValueOnce(new Error("upload failed"));

    await submit("dictated prompt");

    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/chat-audio",
      expect.any(FormData),
      expect.any(Object),
    );
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body).toMatchObject({
      message: "dictated prompt",
      audio_artifact_id: null,
    });
  });

  it("queues a second submit instead of starting a concurrent stream", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    await submit("second message");

    // The synchronous in-flight ref must prevent a second concurrent stream.
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.getByText("1 message queued")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Edit queued message" }),
    ).toBeTruthy();
  });

  it("drains the queued message into one new stream after the turn ends", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await submit("second message");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expect(streamCalls[1]?.body.message).toBe("second message");
    // The queued bubble is reused on drain — not duplicated.
    expect(screen.getAllByText("second message")).toHaveLength(1);
  });

  it("Escape in the composer edits the queued message, preserving the active run", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    await submit("second message");

    const ambientEscape = vi.fn();
    window.addEventListener("keydown", ambientEscape);
    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Escape" });
    });
    window.removeEventListener("keydown", ambientEscape);

    // Consumed at the composer, so sibling window/document Escape listeners never see it.
    expect(ambientEscape).not.toHaveBeenCalled();

    // Queued text returns to the input; the run was not cancelled.
    expect(textarea().value).toBe("second message");
    expect(textarea().disabled).toBe(false);
    expect(cancelPost).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Queue for next turn" }),
    ).toBeTruthy();
  });

  it("an IME Escape in the composer does not discard the queued message", async () => {
    // Dismissing a conversion candidate is not abandoning the follow-up. The composer
    // handler consumes Escape before the window guard can see it, so it has to make the
    // composition check itself — otherwise the queued text is dropped with no bubble
    // and no text, because the half-composed input wins the restore tiebreak.
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    await submit("second message");

    fireEvent.change(textarea(), { target: { value: "にほんご" } });
    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Escape", isComposing: true });
    });

    // The queued message survives, and the composition text is left alone.
    expect(textarea().value).toBe("にほんご");
    expect(screen.getAllByText("second message").length).toBeGreaterThan(0);
    expect(cancelPost).not.toHaveBeenCalled();
  });

  it("an Escape pressed during IME composition never cancels the turn", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();
    expect(document.activeElement).toBe(outside);

    // Dismissing a conversion candidate, not stopping the turn.
    await act(async () => {
      fireEvent.keyDown(window, { key: "Escape", isComposing: true });
      fireEvent.keyDown(outside, { key: "Escape", isComposing: true });
    });

    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    outside.remove();
  });

  it("Escape outside the composer stops the turn and records the gesture", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();

    await act(async () => {
      fireEvent.keyDown(window, { key: "Escape" });
    });

    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.objectContaining({
        cancel_token: expect.any(String),
        source: "escape_key",
      }),
    );
    outside.remove();
  });

  it("the stop control does not cancel before the turn's first frame arrives", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Sent, but no SSE frame has been delivered to the reducer yet: the control is
    // mounted and pressable, so a click on it is the negative arm.
    const pending = screen.getByRole("button", { name: "Starting…" });
    expect(pending.hasAttribute("disabled")).toBe(false);
    expect(pending.getAttribute("aria-busy")).not.toBe("true");
    await act(async () => {
      fireEvent.click(pending);
    });
    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
    );

    await deliverFirstFrame();

    const stop = screen.getByRole("button", { name: "Stop" });
    await act(async () => {
      fireEvent.click(stop);
    });

    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.objectContaining({
        cancel_token: expect.any(String),
        source: "stop_button",
      }),
    );
  });

  it("arms the visible stop shortly after send even when no frame ever streams", async () => {
    // The gate exists for the second click of a double-tap, not to leave the control a
    // user reaches for dead while a turn hangs before its first frame.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await renderChat();
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Starting…" }));
      });
      expect(cancelPost).not.toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.anything(),
      );

      // Past the double-tap window, with no frame delivered at any point.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.objectContaining({ source: "stop_button" }),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not let a stalled turn's arming deadline arm the turn its queued prompt drains into", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await renderChat();
      await submit("first message");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await submit("second message");

      // Stop just short of the first turn's arming deadline, hand over to the turn its
      // queued prompt drains into, then cross that deadline: it belongs to the turn that
      // scheduled it and must not arm the one now running.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400);
      });
      await completeOldestStream("first done");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Starting…" }));
      });
      expect(cancelPost).not.toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.anything(),
      );

      await deliverFirstFrame();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.anything(),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the queued block-build target on cancel so it cannot leak into the next message", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Arm a block-level Generate while the turn is in flight: it queues behind
    // the active turn, capturing the target block label in a ref.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // Cancel the queued block-build before it sends.
    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Escape" });
    });

    // Finish the original turn and send an unrelated follow-up.
    await completeOldestStream("first done");
    await submit("a normal follow-up");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    const followUp = streamCalls.find(
      (call) => call.body.message === "a normal follow-up",
    );
    expect(followUp).toBeTruthy();
    expect(
      (followUp!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBeNull();
  });

  it("keeps the block generating label set while its build waits behind an in-flight turn", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Arm a block-level Generate while a turn is in flight: it queues behind it.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(useCopilotActionStore.getState().generatingBlockLabel).toBe(
      "open_page",
    );

    // The unrelated turn ends; the queued block build then drains into its own
    // stream. The generating label must survive both events.
    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(useCopilotActionStore.getState().generatingBlockLabel).toBe(
      "open_page",
    );
    expect(
      (streamCalls[1]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBe("open_page");

    // The label clears only once the block-build turn itself finishes.
    await act(async () => {
      streamCalls[1]!.onMessage(terminalResponse("block rebuilt"));
      streamCalls[1]!.resolve();
    });
    await waitFor(() =>
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull(),
    );
  });

  it("does not arm a block-build target when its generate no-ops behind a queued prompt", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Queue a normal follow-up behind the in-flight turn.
    await submit("second message");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // A block Generate now no-ops (a prompt is already queued); it must neither
    // arm the block target nor leave the block stuck generating.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    await waitFor(() =>
      expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull(),
    );
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // The queued follow-up drains normally, unscoped to any block.
    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    const drained = streamCalls.find(
      (call) => call.body.message === "second message",
    );
    expect(drained).toBeTruthy();
    expect(
      (drained!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBeNull();
  });

  it("drops a queued block build when its Stop is pressed, sparing the active turn", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Arm a block Generate while the turn is in flight: it queues behind it.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(useCopilotActionStore.getState().generatingBlockLabel).toBe(
      "open_page",
    );

    // Press the block's Stop: drop the queued build without cancelling the
    // unrelated in-flight turn.
    await act(async () => {
      useCopilotActionStore.getState().requestCancel();
    });
    expect(useCopilotActionStore.getState().generatingBlockLabel).toBeNull();
    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
    );

    // The original turn completes; the dropped build must not drain into a stream.
    await completeOldestStream("first done");
    await act(async () => {});
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("resets the live narrative when a stream throws without a terminal", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await act(async () => {
      streamCalls[0]?.onMessage(turnStart());
    });
    // The live (non-terminal) narrative bubble is an aria-live status region.
    expect(screen.queryAllByRole("status").length).toBeGreaterThan(0);

    await act(async () => {
      streamCalls[0]?.reject(new Error("network drop"));
    });

    // Resetting the narrative stops the progress/elapsed indicator from
    // ticking forever beside the error message.
    expect(screen.queryAllByRole("status")).toHaveLength(0);
    expect(
      screen.getByText(/Copilot is checking whether this turn finished/),
    ).toBeTruthy();
  });

  it("renders a response-only error narrative payload as halted", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");

    await act(async () => {
      call.onMessage({
        ...terminalResponse(
          "Copilot hit an internal error before it could finish this turn.",
        ),
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: false,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "error",
          terminalMessage:
            "Copilot hit an internal error before it could finish this turn.",
          narrativeSummary: "Copilot hit an internal error.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: null,
          endedAt: null,
        },
      });
      call.resolve();
    });

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(screen.getByText("Copilot hit an internal error.")).toBeTruthy();
    expect(screen.queryByText("Completed the run")).toBeNull();
  });

  it("keeps proposal actions after user-cancelled turns with staged drafts", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");

    await act(async () => {
      call.onMessage(turnStart());
      call.onMessage(workflowDraft());
      call.onMessage({
        ...terminalResponse(
          "Cancelled. I have a draft workflow you can keep -- accept it to save, or discard.",
        ),
        updated_workflow: { workflow_id: "wf_draft" },
        proposal_disposition: "review_untested",
        cancelled: true,
      });
      call.resolve();
    });

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.getByText(/Cancelled\. I have a draft workflow/),
    ).toBeTruthy();
    expect(screen.queryByText("Run halted")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("shows budget-halted draft turns as reviewable draft state", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");

    await act(async () => {
      call.onMessage(turnStart());
      call.onMessage(workflowDraft());
      call.onMessage({
        type: "block_progress",
        workflow_run_block_id: "wrb_add_to_cart",
        block_label: "add_to_cart",
        block_type: "task",
        status: "canceled",
        iteration: 1,
        timestamp: "2026-05-25T00:00:04Z",
      });
      call.onMessage({
        ...terminalResponse(
          "The draft made progress but the test exceeded its tool budget. Review the draft before accepting it.",
        ),
        updated_workflow: { workflow_id: "wf_draft" },
        proposal_disposition: "review_untested",
      });
      call.resolve();
    });

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(screen.getByText(/draft made progress/)).toBeTruthy();
    expect(screen.queryByText("Run halted")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("does not show proposal actions after cancelled turns without a draft", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");

    await act(async () => {
      call.onMessage(turnStart());
      call.onMessage({
        ...terminalResponse("Stopped. 0 blocks ran this turn."),
        proposal_disposition: "no_proposal",
        cancelled: true,
      });
      call.resolve();
    });

    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("hydrates cancelled pending draft controls from chat history", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "user",
          content: "build me a workflow",
          created_at: "2026-05-25T00:00:00Z",
        },
        {
          sender: "ai",
          content:
            "Cancelled. I have a draft workflow you can keep -- accept it to save, or discard.",
          created_at: "2026-05-25T00:00:05Z",
          narrative_payload: {
            turnId: "turn-1",
            turnIndex: 0,
            mode: "build",
            responseType: "REPLY",
            cancelled: true,
            proposalDisposition: "review_untested",
            designStarted: true,
            designEnded: true,
            draft: {
              blockCount: 2,
              blockLabels: ["open_page", "add_to_cart"],
              summary: null,
            },
            blocks: [
              {
                workflowRunBlockId: "",
                label: "open_page",
                blockType: "goto_url",
                state: "drafted",
                lastSeenIteration: 0,
                activity: [],
                startedAt: null,
                endedAt: null,
              },
            ],
            terminal: "response",
            terminalMessage:
              "Cancelled. I have a draft workflow you can keep -- accept it to save, or discard.",
            narrativeSummary:
              "Cancelled. I have a draft workflow you can keep -- accept it to save, or discard.",
            priorBlockCount: null,
            designActivity: [],
            startedAt: "2026-05-25T00:00:00Z",
            endedAt: "2026-05-25T00:00:05Z",
          },
        },
      ],
      proposed_workflow: { workflow_id: "wf_draft" },
      auto_accept: false,
    };

    await renderChat();

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.getByText(/Cancelled\. I have a draft workflow/),
    ).toBeTruthy();
    expect(screen.queryByText("Run halted")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("keeps a rejected history-loaded auto-applicable draft labeled as proposed changes", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "user",
          content: "build me a workflow",
          created_at: "2026-05-25T00:00:00Z",
        },
        {
          sender: "ai",
          content: "I drafted workflow changes for review.",
          created_at: "2026-05-25T00:00:05Z",
          narrative_payload: {
            turnId: "turn-1",
            turnIndex: 0,
            mode: "build",
            responseType: "REPLY",
            cancelled: false,
            proposalDisposition: "auto_applicable",
            designStarted: true,
            designEnded: true,
            draft: {
              blockCount: 1,
              blockLabels: ["open_page"],
              summary: null,
            },
            blocks: [
              {
                workflowRunBlockId: "",
                label: "open_page",
                blockType: "goto_url",
                state: "drafted",
                lastSeenIteration: 0,
                activity: [],
                startedAt: null,
                endedAt: null,
              },
            ],
            terminal: "response",
            terminalMessage: "I drafted workflow changes for review.",
            narrativeSummary: "I drafted workflow changes for review.",
            priorBlockCount: null,
            designActivity: [],
            startedAt: "2026-05-25T00:00:00Z",
            endedAt: "2026-05-25T00:00:05Z",
          },
        },
      ],
      proposed_workflow: { workflow_id: "wf_draft" },
      auto_accept: false,
    };

    const portalTarget = document.createElement("div");
    document.body.appendChild(portalTarget);
    await renderChat({ docked: true, portalTarget });

    expect(screen.getByText("Proposed changes")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText("Proposed changes")).toBeTruthy();
    expect(screen.queryByText("Applied changes")).toBeNull();
    portalTarget.remove();
  });

  it("relabels an auto-applicable draft as applied changes once accepted", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "user",
          content: "build me a workflow",
          created_at: "2026-05-25T00:00:00Z",
        },
        {
          sender: "ai",
          content: "I drafted workflow changes for review.",
          created_at: "2026-05-25T00:00:05Z",
          narrative_payload: {
            turnId: "turn-1",
            turnIndex: 0,
            mode: "build",
            responseType: "REPLY",
            cancelled: false,
            proposalDisposition: "auto_applicable",
            designStarted: true,
            designEnded: true,
            draft: {
              blockCount: 1,
              blockLabels: ["open_page"],
              summary: null,
            },
            blocks: [
              {
                workflowRunBlockId: "",
                label: "open_page",
                blockType: "goto_url",
                state: "drafted",
                lastSeenIteration: 0,
                activity: [],
                startedAt: null,
                endedAt: null,
              },
            ],
            terminal: "response",
            terminalMessage: "I drafted workflow changes for review.",
            narrativeSummary: "I drafted workflow changes for review.",
            priorBlockCount: null,
            designActivity: [],
            startedAt: "2026-05-25T00:00:00Z",
            endedAt: "2026-05-25T00:00:05Z",
          },
        },
      ],
      proposed_workflow: { workflow_id: "wf_draft" },
      auto_accept: false,
    };

    const portalTarget = document.createElement("div");
    document.body.appendChild(portalTarget);
    await renderChat({ docked: true, portalTarget });

    expect(screen.getByText("Proposed changes")).toBeTruthy();
    expect(screen.queryByText("Applied changes")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/apply-proposed-workflow",
      expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
    );
    expect(screen.getByText("Applied changes")).toBeTruthy();
    expect(screen.queryByText("Proposed changes")).toBeNull();
    portalTarget.remove();
  });

  it("renders an ASK_QUESTION response payload as a question", async () => {
    await renderChat();
    await submit("build a lookup workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");

    await act(async () => {
      call.onMessage({
        ...terminalResponse("Please provide the exact registry URL."),
        response_type: "ASK_QUESTION",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "diagnose",
          responseType: "ASK_QUESTION",
          designStarted: false,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "Please provide the exact registry URL.",
          narrativeSummary: "Please provide the exact registry URL.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: null,
          endedAt: null,
        },
      });
      call.resolve();
    });

    expect(screen.getByTestId("copilot-terminal-prose")).toBeTruthy();
    expect(screen.queryByText("Needs your input")).toBeNull();
    expect(screen.queryByText("Completed the run")).toBeNull();
  });

  it("renders a legacy diagnose payload asking for input as a question", async () => {
    await renderChat();
    await submit("build a lookup workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");
    const longInputRequest =
      "Please provide the **exact registry URL** you want the workflow to use. I will build a general workflow with a `person_name` input after you provide it.";

    await act(async () => {
      call.onMessage({
        ...terminalResponse(longInputRequest),
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "diagnose",
          designStarted: false,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: longInputRequest,
          narrativeSummary: longInputRequest,
          priorBlockCount: null,
          designActivity: [],
          startedAt: null,
          endedAt: null,
        },
      });
      call.resolve();
    });

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.getByText("exact registry URL", { selector: "strong" }),
    ).toBeTruthy();
    expect(screen.getByText("person_name", { selector: "code" })).toBeTruthy();
    expect(screen.queryByText("Answered")).toBeNull();
    expect(screen.queryByText("Completed the run")).toBeNull();
  });

  it("does not orphan a message on a same-tick double submit while working", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Two synchronous Enter presses before React commits the first queue: the
    // synchronous queuedPromptRef must make the second a no-op, not a 2nd queue.
    await act(async () => {
      fireEvent.change(textarea(), { target: { value: "queued message" } });
      const ta = textarea();
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });

    expect(screen.getAllByText("queued message")).toHaveLength(1);
    expect(postStreaming).toHaveBeenCalledTimes(1);
  });
});

describe("WorkflowCopilotChat — a repeat of the turn's own message is not re-run", () => {
  it("drops a queued prompt identical to the one that opened the finished turn", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("build me a workflow");
    expect(screen.getAllByText("build me a workflow")).toHaveLength(2);

    await completeOldestStream("first done");
    await act(async () => {});

    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("build me a workflow")).toHaveLength(1);
    expect(
      screen.queryByText("Queued — sends when this turn finishes."),
    ).toBeNull();
  });

  it("drains an identical queued prompt when the turn ends in a response-framed error", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");
    await act(async () => {
      call.onMessage({
        ...terminalResponse("Copilot hit an internal error."),
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: false,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "error",
          terminalMessage: "Copilot hit an internal error.",
          narrativeSummary: "Copilot hit an internal error.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: null,
          endedAt: null,
        },
      });
      call.resolve();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("build me a workflow");
  });

  it("drains an identical queued prompt when the turn's tested run failed", async () => {
    await renderChat();
    await submit("run the workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("run the workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");
    await act(async () => {
      call.onMessage({
        ...terminalResponse("The run failed — here's a fix."),
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: false,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "The run failed — here's a fix.",
          narrativeSummary: "The run failed.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: null,
          endedAt: null,
          terminalEnvelope: {
            run_verdict: "not_demonstrated",
            run_display_reason: "The run did not reach the goal.",
          },
        },
      });
      call.resolve();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("run the workflow");
  });

  it("drains an identical queued prompt when a live browser session attached mid-turn", async () => {
    const view = await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    view.rerender(<WorkflowCopilotChat liveBrowserSessionId="pbs_live_1" />);
    await completeOldestStream("first done");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("build me a workflow");
  });

  it("drains an identical queued prompt when the turn comes back cancelled", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");
    await act(async () => {
      call.onMessage({ ...terminalResponse("Stopped."), cancelled: true });
      call.resolve();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("build me a workflow");
  });

  it("drains an identical queued prompt that carries dictation audio", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    speechState.takeAudioBlob.mockReturnValueOnce(new Blob(["dictation"]));
    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("first done");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("build me a workflow");
  });

  it("drains an identical queued prompt when the turn itself opened on dictation audio", async () => {
    await renderChat();
    speechState.takeAudioBlob.mockReturnValueOnce(new Blob(["dictation"]));
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("first done");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("build me a workflow");
  });

  it("drains a queued repeat of the message a targeted block build opened on", async () => {
    await renderChat();
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const blockBuildMessage = streamCalls[0]!.body.message;
    expect(
      (streamCalls[0]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBe("open_page");

    await submit(blockBuildMessage);
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("block rebuilt");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.message).toBe(blockBuildMessage);
    expect(
      (streamCalls[1]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBeNull();
  });

  it("drains an identical queued prompt when the composer left the code mode the turn opened in", async () => {
    await renderChatWithCodeMode();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Switch mode" }), {
      button: 0,
      ctrlKey: false,
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Build"));
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(
      (streamCalls[0]!.body as unknown as { code_block: boolean | null })
        .code_block,
    ).toBe(false);

    await submit("build me a workflow");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // A block-level Generate behind a queued prompt disarms its own target but
    // still flips the composer into code, so the queued send is a new shape.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("first done");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.message).toBe("build me a workflow");
    expect(
      (streamCalls[1]!.body as unknown as { code_block: boolean | null })
        .code_block,
    ).toBe(true);
  });

  it("drains a queued block build that repeats the message of the turn in flight", async () => {
    await renderChat();
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const blockBuildMessage = streamCalls[0]!.body.message;
    await completeOldestStream("block rebuilt");

    // The user re-sends the block build's own text by hand, so this turn is
    // scoped to no block.
    await submit(blockBuildMessage);
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(
      (streamCalls[1]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBeNull();

    // Generate on the same block again: nothing is queued yet, so the target
    // stays armed while its message queues behind the in-flight turn.
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    expect(postStreaming).toHaveBeenCalledTimes(2);

    await act(async () => {
      streamCalls[1]!.onMessage(terminalResponse("hand-typed done"));
      streamCalls[1]!.resolve();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    expect(streamCalls[2]!.body.message).toBe(blockBuildMessage);
    expect(
      (streamCalls[2]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBe("open_page");
  });

  it("drains an identical queued prompt when a block build armed mid-send", async () => {
    await renderChat();
    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const blockBuildMessage = streamCalls[0]!.body.message;
    await completeOldestStream("block rebuilt");

    // Generate lands while the send is still awaiting its client, i.e. after
    // the turn was stamped but before the request is built: this turn carries
    // the block target, and its queued repeat does not.
    vi.mocked(getSseClient).mockImplementationOnce(async () => {
      await act(async () => {
        useCopilotActionStore
          .getState()
          .requestBuild({ blockLabel: "open_page", prompt: "open the page" });
      });
      return { postStreaming } as unknown as Awaited<
        ReturnType<typeof getSseClient>
      >;
    });
    await submit(blockBuildMessage);
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(
      (streamCalls[1]!.body as unknown as { target_block_label: string | null })
        .target_block_label,
    ).toBe("open_page");

    await act(async () => {
      streamCalls[1]!.onMessage(terminalResponse("block rebuilt again"));
      streamCalls[1]!.resolve();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    expect(streamCalls[2]!.body.message).toBe(blockBuildMessage);
  });

  it("drops a queued prompt that a replacement send rewrote into a repeat", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await submit("something else entirely");
    await submit("build me a workflow");

    await completeOldestStream("first done");
    await act(async () => {});

    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("something else entirely")).toBeNull();
    expect(screen.getAllByText("build me a workflow")).toHaveLength(1);
  });
});

describe("WorkflowCopilotChat — a stop never replays a queued message", () => {
  it("hands the queued text back to the composer instead of auto-sending it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    await submit("also add a login step");
    expect(screen.getAllByText("also add a login step")).toHaveLength(1);

    await act(async () => useCopilotActionStore.getState().requestCancel());
    // The turn ends only after the stop lands — this is the edge that used to
    // drain the queue and start a whole new build turn.
    await completeOldestStream("stopped");

    expect(textarea().value).toBe("also add a login step");
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("shows a stopping state on the first press so the control is not pressed repeatedly", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    const stop = screen.getByRole("button", { name: /Stop/ });
    expect(stop.hasAttribute("disabled")).toBe(false);

    await act(async () => {
      fireEvent.click(stop);
    });

    const stopping = screen.getByRole("button", { name: /Stopping/ });
    expect(stopping.hasAttribute("disabled")).toBe(true);
    expect(cancelPost).toHaveBeenCalledTimes(1);

    // A second press cannot reach the handler, so no second cancel is posted.
    await act(async () => {
      fireEvent.click(stopping);
    });
    expect(cancelPost).toHaveBeenCalledTimes(1);

    await completeOldestStream("stopped");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Stopping/ })).toBeNull(),
    );
  });
});

describe("WorkflowCopilotChat — the composer stays usable while a prompt is parked", () => {
  // A turn parked on a credential/2FA ask holds the stream open, so isLoading
  // never flips and the queued prompt never drains. Disabling the composer on
  // a queued prompt therefore locked the user out exactly when they had the
  // code the copilot was waiting for.
  it("keeps the textarea typable while a prompt is queued", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await submit("first answer");

    expect(textarea().disabled).toBe(false);
    expect(
      screen.getByPlaceholderText("Type to replace the queued message…"),
    ).toBeTruthy();
  });

  it("does not clobber half-typed composer text when a stop returns the queued one", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    await submit("queued answer");
    // Half-typed replacement, never submitted.
    fireEvent.change(textarea(), {
      target: { value: "half typed replacement" },
    });

    await act(async () => useCopilotActionStore.getState().requestCancel());

    expect(textarea().value).toBe("half typed replacement");
  });

  it("replaces the parked prompt rather than swallowing the second send", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await submit("wrong code 000000");
    await submit("correct code 123456");

    expect(screen.queryByText("wrong code 000000")).toBeNull();
    expect(screen.getAllByText("correct code 123456")).toHaveLength(1);
    // Still exactly one parked prompt, and still no second stream.
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.message).toBe("correct code 123456");
  });
});
