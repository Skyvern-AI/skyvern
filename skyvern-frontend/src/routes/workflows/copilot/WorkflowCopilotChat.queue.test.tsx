import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

async function renderChat() {
  const view = render(<WorkflowCopilotChat />);
  // Let the mount-time chat-history fetch settle.
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
  it("leaves the input enabled while a turn is in flight", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(textarea().disabled).toBe(false);
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeTruthy();
  });

  it("labels the in-flight follow-up action as the next send", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(
      screen.getByText(
        "Copilot is working. Your next send will wait for the next turn.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByPlaceholderText("Type a message to send next…"),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send next" })).toBeTruthy();
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

    await submit("second message");

    // The synchronous in-flight ref must prevent a second concurrent stream.
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(
      screen.getAllByText("Queued — sends when this turn finishes.").length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit queued" })).toBeTruthy();
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

  it("Escape edits the queued message first, preserving the active run", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await submit("second message");
    expect(textarea().disabled).toBe(true);

    await act(async () => {
      fireEvent.keyDown(window, { key: "Escape" });
    });

    // Queued text returns to the input; the run was not cancelled.
    expect(textarea().value).toBe("second message");
    expect(textarea().disabled).toBe(false);
    expect(cancelPost).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeTruthy();
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
      fireEvent.keyDown(window, { key: "Escape" });
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
    expect(screen.getByText(/I encountered an error/)).toBeTruthy();
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

    expect(screen.getByText("Run halted")).toBeTruthy();
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

    expect(screen.getByText("Stopped with a draft")).toBeTruthy();
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

    expect(screen.getByText("Stopped with a draft")).toBeTruthy();
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
        ...terminalResponse("Cancelled by user."),
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

    expect(screen.getByText("Stopped with a draft")).toBeTruthy();
    expect(screen.queryByText("Run halted")).toBeNull();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
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

    expect(screen.getByText("Question")).toBeTruthy();
    expect(screen.queryByText("Completed the run")).toBeNull();
  });

  it("renders a legacy diagnose payload asking for input as a question", async () => {
    await renderChat();
    await submit("build a lookup workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream to complete");
    const longInputRequest =
      "Please provide the exact BACB lookup/registry URL you want the workflow to use. I will build a general workflow with a person_name input after you provide it.";

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

    expect(screen.getByText("Question")).toBeTruthy();
    expect(screen.getByText(longInputRequest)).toBeTruthy();
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
