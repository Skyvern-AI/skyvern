import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

// Capture every postStreaming call so a test can assert how many streams
// started and drive each one to a terminal frame on demand.
type StreamCall = {
  body: { message: string };
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};
const { streamCalls, postStreaming, cancelPost } = vi.hoisted(() => {
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
  return { streamCalls: calls, postStreaming: streaming, cancelPost: post };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockResolvedValue({
      data: {
        workflow_copilot_chat_id: null,
        chat_history: [],
        proposed_workflow: null,
        auto_accept: false,
      },
    }),
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

async function renderChat() {
  const view = render(<WorkflowCopilotChat />);
  // Let the mount-time chat-history fetch settle.
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/Type your message/)).toBeTruthy(),
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
