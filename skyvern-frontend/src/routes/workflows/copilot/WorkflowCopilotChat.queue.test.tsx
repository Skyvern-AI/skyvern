import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect, useState, type ComponentProps } from "react";
import { flushSync } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
} from "./WorkflowCopilotChat";
import { toast } from "@/components/ui/use-toast";

import { getSseClient } from "@/api/sse";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

// Capture every postStreaming call so a test can assert how many streams
// started and drive each one to a terminal frame on demand.
type StreamCall = {
  body: {
    message: string;
    attached_file_ids?: string[];
    target_block_label?: string | null;
    product_action?: string | null;
  };
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};
const {
  streamCalls,
  postStreaming,
  cancelPost,
  uploadCount,
  uploadGate,
  deleteFile,
  historyResponse,
  speechState,
} = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const uploadCount = { n: 0 };
  const deleteFile = vi.fn().mockResolvedValue({});
  const uploadGate = { hold: false, release: [] as (() => void)[] };
  const post = vi.fn((path: string) => {
    if (path !== "/upload_file") return Promise.resolve({});
    const payload = { data: { file_id: `file_${++uploadCount.n}` } };
    if (!uploadGate.hold) return Promise.resolve(payload);
    // Held open so a test can observe a send while the upload is still in flight.
    return new Promise((resolve) => {
      uploadGate.release.push(() => resolve(payload));
    });
  });
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
    uploadCount,
    uploadGate,
    deleteFile,
    historyResponse: history,
    speechState: speech,
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

const pageExitDelete = vi.hoisted(() => vi.fn().mockResolvedValue(true));

vi.mock("@/api/AxiosClient", () => ({
  deleteUploadedFileOnPageExit: pageExitDelete,
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation(() => Promise.resolve(historyResponse)),
    post: cancelPost,
    delete: deleteFile,
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

vi.mock("@/store/WorkflowHasChangesStore", () => {
  const state = {
    getSaveData: () => saveData,
    setSaveBlockedReason: () => {},
  };
  return {
    useWorkflowHasChangesStore: Object.assign(() => state, {
      getState: () => state,
    }),
  };
});

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

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

function HomeHandoffChat({
  isLiveBrowserReady,
  docked = false,
}: {
  isLiveBrowserReady: boolean;
  docked?: boolean;
}) {
  const [initialMessage, setInitialMessage] = useState<string | undefined>(
    "Create a workflow that opens example.com",
  );

  return (
    <WorkflowCopilotChat
      initialMessage={initialMessage}
      onInitialMessageConsumed={() => setInitialMessage(undefined)}
      requiresLiveBrowser
      isLiveBrowserReady={isLiveBrowserReady}
      liveBrowserSessionId={isLiveBrowserReady ? "pbs_live_1" : null}
      docked={docked}
      portalTarget={docked ? document.body : undefined}
    />
  );
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

async function attachSpreadsheet(
  filename: string,
  file = new File(["row_id,url\n"], filename),
) {
  const input = document.querySelector(
    'input[accept*=".pdf"]',
  ) as HTMLInputElement;
  if (!input) throw new Error("composer has no attachment input");
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
}

async function dropAttachments(...files: File[]) {
  const composer = screen.getByRole("group", {
    name: "Copilot message composer",
  });
  const dataTransfer = {
    files,
    types: ["Files"],
    dropEffect: "none",
  };
  let dropWasNotCancelled = true;
  await act(async () => {
    dropWasNotCancelled = fireEvent.drop(composer, { dataTransfer });
  });
  return dropWasNotCancelled;
}

function oversizeFile(filename: string) {
  const file = new File(["x"], filename);
  Object.defineProperty(file, "size", { value: 11 * 1024 * 1024 });
  return file;
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
  uploadCount.n = 0;
  uploadGate.hold = false;
  uploadGate.release.length = 0;
  deleteFile.mockClear();
  pageExitDelete.mockClear();
  pageExitDelete.mockResolvedValue(true);
  vi.mocked(toast).mockClear();
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
  saveData.workflow.workflow_id = "wf_1";
  useCopilotActionStore.setState({
    pendingBuild: null,
    generatingBlockLabel: null,
    cancelNonce: 0,
  });
});

afterEach(() => {
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

describe("WorkflowCopilotChat — keep the chat live during a turn", () => {
  it("shows a stable file-drop affordance across nested drag events", async () => {
    await renderChat();
    const composer = screen.getByRole("group", {
      name: "Copilot message composer",
    });
    const dataTransfer = {
      files: [new File(["row_id,url\n"], "rows.csv")],
      types: ["Files"],
      dropEffect: "none",
    };

    fireEvent.dragEnter(composer, { dataTransfer });
    fireEvent.dragEnter(textarea(), { dataTransfer });
    expect(screen.getByText("Drop files to attach")).toBeTruthy();

    fireEvent.dragLeave(textarea(), { dataTransfer });
    expect(screen.getByText("Drop files to attach")).toBeTruthy();

    fireEvent.dragLeave(composer, { dataTransfer });
    expect(screen.queryByText("Drop files to attach")).toBeNull();
  });

  it("uploads supported files dropped on the composer through the attachment flow", async () => {
    await renderChat();

    const dropWasNotCancelled = await dropAttachments(
      new File(["row_id,url\n"], "rows.csv"),
      new File(["%PDF"], "report.pdf"),
    );

    expect(dropWasNotCancelled).toBe(false);
    await waitFor(() => expect(uploadCount.n).toBe(2));
    expect(
      screen.getByRole("button", { name: "Remove rows.csv" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove report.pdf" }),
    ).toBeTruthy();
    expect(screen.queryByText("Drop files to attach")).toBeNull();
  });

  it("offers and uploads demonstration video attachments", async () => {
    await renderChat();

    const input = document.querySelector(
      'input[type="file"][accept*=".csv"]',
    ) as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.accept.split(",")).toEqual(
      expect.arrayContaining([".mp4", ".webm", ".mov"]),
    );

    await dropAttachments(
      new File(["video"], "demo.MP4", { type: "video/mp4" }),
      new File(["video"], "demo.webm", { type: "video/webm" }),
      new File(["video"], "demo.mov", { type: "video/quicktime" }),
    );

    await waitFor(() => expect(uploadCount.n).toBe(3));
    expect(
      screen.getByRole("button", { name: "Remove demo.MP4" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove demo.webm" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove demo.mov" }),
    ).toBeTruthy();
    expect(vi.mocked(toast)).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Unsupported file type" }),
    );
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Use a non-sensitive video" }),
    );
  });

  it("allows videos up to the upload service's 30MB limit", async () => {
    await renderChat();
    const accepted = new File(["video"], "five-minutes.mp4", {
      type: "video/mp4",
    });
    Object.defineProperty(accepted, "size", { value: 20 * 1024 * 1024 });
    const rejected = new File(["video"], "too-large.mp4", {
      type: "video/mp4",
    });
    Object.defineProperty(rejected, "size", { value: 30 * 1024 * 1024 + 1 });

    await dropAttachments(accepted, rejected);

    await waitFor(() => expect(uploadCount.n).toBe(1));
    expect(
      screen.getByRole("button", { name: "Remove five-minutes.mp4" }),
    ).toBeTruthy();
    expect(screen.getByText("over 30MB")).toBeTruthy();
  });

  it("ignores unsupported dropped files before upload", async () => {
    await renderChat();

    await dropAttachments(new File(["hello"], "notes.txt"));

    expect(uploadCount.n).toBe(0);
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Unsupported file type" }),
    );
  });

  it("ignores dragged prose and other non-file payloads", async () => {
    await renderChat();
    const composer = screen.getByRole("group", {
      name: "Copilot message composer",
    });
    const dataTransfer = {
      files: [],
      types: ["text/plain"],
      dropEffect: "none",
    };

    expect(fireEvent.dragEnter(composer, { dataTransfer })).toBe(true);
    expect(screen.queryByText("Drop files to attach")).toBeNull();
    expect(fireEvent.drop(composer, { dataTransfer })).toBe(true);
    expect(uploadCount.n).toBe(0);
  });

  it("admits only the remaining attachment slots from a multi-file drop", async () => {
    await renderChat();
    for (let index = 0; index < 19; index += 1) {
      await attachSpreadsheet(`picked-${index}.csv`);
    }

    await dropAttachments(
      new File(["row_id,url\n"], "accepted.csv"),
      new File(["row_id,url\n"], "over-limit.csv"),
    );

    await waitFor(() => expect(uploadCount.n).toBe(20));
    expect(
      screen.getByRole("button", { name: "Remove accepted.csv" }),
    ).toBeTruthy();
    expect(screen.queryByText("over-limit.csv")).toBeNull();
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Too many files" }),
    );
  });

  it("does not let an oversized drop consume a remaining attachment slot", async () => {
    await renderChat();
    for (let index = 0; index < 19; index += 1) {
      await attachSpreadsheet(`picked-${index}.csv`);
    }

    await dropAttachments(
      new File([new Uint8Array(10 * 1024 * 1024 + 1)], "oversized.csv"),
      new File(["row_id,url\n"], "accepted.csv"),
    );

    await waitFor(() => expect(uploadCount.n).toBe(20));
    expect(
      screen.getByRole("button", { name: "Remove accepted.csv" }),
    ).toBeTruthy();
    expect(screen.getByText("oversized.csv")).toBeTruthy();
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "File too large" }),
    );
    expect(vi.mocked(toast)).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Too many files" }),
    );
  });

  it("keeps the plus-button picker available for attachment uploads", async () => {
    await renderChat();

    expect(
      (
        screen.getByRole("button", {
          name: "Attach a file",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    await attachSpreadsheet("picked.csv");

    await waitFor(() => expect(uploadCount.n).toBe(1));
    expect(
      screen.getByRole("button", { name: "Remove picked.csv" }),
    ).toBeTruthy();
  });

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

  it("shows a Home handoff prompt before the Copilot is ready", async () => {
    const prompt = "Create a workflow that opens example.com";
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Ready to build your workflow.",
        created_at: "2026-05-25T00:00:00Z",
      },
    ];
    const view = render(<HomeHandoffChat isLiveBrowserReady={false} />);

    expect(screen.getByText(prompt)).toBeTruthy();

    await waitFor(() =>
      expect(
        screen.getByText("Prompt queued. Waiting for live browser..."),
      ).toBeTruthy(),
    );
    expect(screen.getAllByText(prompt)).toHaveLength(1);

    view.rerender(<HomeHandoffChat isLiveBrowserReady />);

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.message).toBe(prompt);
    expect(screen.getAllByText(prompt)).toHaveLength(1);

    await completeOldestStream("Workflow built.");
    expect(screen.getAllByText(prompt)).toHaveLength(1);
    const promptBubble = screen.getByText(prompt);
    const responseBubble = screen.getByText("Workflow built.");
    expect(
      promptBubble.compareDocumentPosition(responseBubble) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("drains a queued Home handoff prompt once when a sync render lands mid-drain", async () => {
    const prompt = "Create a workflow that opens example.com";
    // Every send attempt flushes the editor draft before reserving its turn.
    const flushDraft = vi.fn();
    useWorkflowYamlEditorStore.setState({ flushDraft });
    // The parent's sync render lands before the drain's own clear commits and hands
    // the chat a new handleSend, re-running the drain effect with the stale prompt.
    function RenderedMidDrain({ ready }: { ready: boolean }) {
      const [initialMessage, setInitialMessage] = useState<string | undefined>(
        prompt,
      );
      const [, bump] = useState(0);
      useEffect(() => {
        if (ready) flushSync(() => bump((n) => n + 1));
      }, [ready]);
      return (
        <WorkflowCopilotChat
          initialMessage={initialMessage}
          onInitialMessageConsumed={() => setInitialMessage(undefined)}
          onWorkflowPersisted={() => {}}
          requiresLiveBrowser
          isLiveBrowserReady={ready}
          liveBrowserSessionId={ready ? "pbs_live_1" : null}
        />
      );
    }
    const view = render(<RenderedMidDrain ready={false} />);
    await waitFor(() =>
      expect(
        screen.getByText("Prompt queued. Waiting for live browser..."),
      ).toBeTruthy(),
    );

    view.rerender(<RenderedMidDrain ready />);
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    expect(flushDraft).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: "Edit queued message" }),
    ).toBeNull();
    useWorkflowYamlEditorStore.setState({ flushDraft: undefined });
  });

  it("does not carry the Home handoff prompt into another history chat", async () => {
    const prompt = "Create a workflow that opens example.com";
    render(<HomeHandoffChat isLiveBrowserReady={false} docked />);

    await waitFor(() =>
      expect(
        screen.getByText("Prompt queued. Waiting for live browser..."),
      ).toBeTruthy(),
    );
    historyResponse.data = {
      ...historyResponse.data,
      workflow_copilot_chat_id: "chat-2",
      chat_history: [
        {
          sender: "ai",
          content: "This belongs to a different chat.",
          created_at: "2026-05-25T00:01:00Z",
        },
      ],
    };

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

    expect(screen.queryByText(prompt)).toBeNull();
    expect(screen.getByText("This belongs to a different chat.")).toBeTruthy();
  });

  it("keeps the Home handoff prompt during a same-chat reload", async () => {
    const prompt = "Create a workflow that opens example.com";
    const workflowId = saveData.workflow.workflow_id;
    saveData.workflow.workflow_id = "";
    historyResponse.data = {
      ...historyResponse.data,
      workflow_copilot_chat_id: "chat-1",
    };
    Object.assign(historyResponse.data, {
      question_interactions: [
        {
          interaction_id: "interaction-1",
          turn_id: "turn-1",
          tool_call_id: "call-1",
          status: "pending",
          response: null,
          created_at: "2026-05-25T00:00:00Z",
          resolved_at: null,
          parts: [
            {
              part_id: "part-1",
              prompt: "Which site?",
              choices: [],
            },
          ],
        },
      ],
      pending_question_cancel_token: "cancel-1",
    });

    render(<HomeHandoffChat isLiveBrowserReady={false} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel question" }),
      ).toBeTruthy(),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel question" }));
    });

    expect(screen.getByText(prompt)).toBeTruthy();
    saveData.workflow.workflow_id = workflowId;
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

  it("sends a queued message with the file that was attached to it, not a later one", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // A file picked while the message is still queued belongs to the NEXT message.
    await attachSpreadsheet("later.csv");

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_1"]);
    // Only the composer chip carries a remove control, so this asserts the later file is
    // still in the tray waiting for the message it belongs to.
    expect(
      screen.getByRole("button", { name: "Remove later.csv" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Remove queued.csv" }),
    ).toBeNull();
  });

  it("still drains a text-only queued message while a later attachment is uploading", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // No file on the queued message: the case where the drain carries nothing of its own.
    await submit("plain follow-up");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    uploadGate.hold = true;
    await attachSpreadsheet("later.csv");

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("plain follow-up");
    expect(streamCalls[1]?.body.attached_file_ids ?? []).toEqual([]);
  });

  it("still drains a queued message while a later attachment is uploading", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // The next message's file is still uploading when the turn ends. The drain has already
    // consumed the queued prompt, so refusing it here would lose that message outright.
    uploadGate.hold = true;
    await attachSpreadsheet("later.csv");

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expect(streamCalls[1]?.body.message).toBe("parse the queued sheet");
    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_1"]);
  });

  it("keeps the next message's file when a queued message is re-queued for the browser", async () => {
    const view = await renderChat({
      requiresLiveBrowser: true,
      isLiveBrowserReady: true,
    });
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // Staged for the message after the queued one.
    await attachSpreadsheet("next.csv");

    // The browser drops before the turn ends, so the drain re-queues the message instead of
    // sending it. That re-queue carries its own file and must not touch the tray.
    view.rerender(
      <WorkflowCopilotChat requiresLiveBrowser isLiveBrowserReady={false} />,
    );
    await completeOldestStream("first done");

    await waitFor(() =>
      expect(
        screen.getByText("Prompt queued. Waiting for live browser..."),
      ).toBeTruthy(),
    );
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Remove next.csv" }),
    ).toBeTruthy();
  });

  it("does not turn the send button into Stop while a file is still uploading", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();

    // The upload has not finished, so the file is visible in the tray but not yet staged.
    uploadGate.hold = true;
    await attachSpreadsheet("mine.csv");

    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    const cancelled = cancelPost.mock.calls.some((call) =>
      String(call[0]).includes("cancel"),
    );
    expect(cancelled).toBe(false);
  });

  it("does not turn the send button into Stop while only a file is staged", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();

    await attachSpreadsheet("mine.csv");

    // With a file staged the control must read as a queue/send affordance, not Stop —
    // otherwise a user who attaches mid-turn and clicks cancels the turn they were adding to.
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Queue for next turn" }),
      );
    });
    const cancelled = cancelPost.mock.calls.some((call) =>
      String(call[0]).includes("cancel"),
    );
    expect(cancelled).toBe(false);
  });

  it("shows an oversize file as a failed chip without uploading it", async () => {
    await renderChat();
    const big = oversizeFile("scan.png");

    await attachSpreadsheet("scan.png", big);

    expect(screen.getByText("scan.png")).toBeTruthy();
    expect(screen.getByText("over 10MB")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove scan.png" }),
    ).toBeTruthy();
    const uploaded = cancelPost.mock.calls.some(
      (call) => call[0] === "/upload_file",
    );
    expect(uploaded).toBe(false);

    await submit("read the scan");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("over 10MB")).toBeNull();
  });

  it("drops a failed chip when its message is queued", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const big = oversizeFile("scan.png");
    await attachSpreadsheet("scan.png", big);

    await submit("read the scan next");

    expect(
      screen.getByRole("button", { name: "Edit queued message" }),
    ).toBeTruthy();
    expect(screen.queryByText("over 10MB")).toBeNull();
  });

  it("drops a failed chip when it replaces a queued message", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await submit("queued message");
    const big = oversizeFile("scan.png");
    await attachSpreadsheet("scan.png", big);

    await submit("read the scan instead");

    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(screen.getByText("read the scan instead")).toBeTruthy();
    expect(screen.queryByText("over 10MB")).toBeNull();
  });

  it("says a file staged before a question will not go with the answer", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await attachSpreadsheet("mine.csv");

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream");
    await act(async () => {
      call.onMessage({
        type: "question_required",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "wcc_1",
        cancel_token: null,
        interactions: [
          {
            interaction_id: "qi_1",
            turn_id: "turn-1",
            tool_call_id: "tc_1",
            parts: [{ part_id: "p1", prompt: "Which column?", choices: [] }],
            status: "pending",
            response: null,
            created_at: "2026-01-01T00:00:00Z",
            resolved_at: null,
          },
        ],
      });
    });

    // The answer path sends text only, so the staged file must not read as part of it.
    expect(
      screen.getByText(
        "Attached files will be sent with your next message, not with your answer.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();
  });

  it("says a file still uploading when a question arrives will not go with the answer", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    uploadGate.hold = true;
    await attachSpreadsheet("inflight.csv");

    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream");
    await act(async () => {
      call.onMessage({
        type: "question_required",
        turn_id: "turn-1",
        workflow_copilot_chat_id: "wcc_1",
        cancel_token: null,
        interactions: [
          {
            interaction_id: "qi_1",
            turn_id: "turn-1",
            tool_call_id: "tc_1",
            parts: [{ part_id: "p1", prompt: "Which column?", choices: [] }],
            status: "pending",
            response: null,
            created_at: "2026-01-01T00:00:00Z",
            resolved_at: null,
          },
        ],
      });
    });

    expect(
      screen.getByText(
        "Attached files will be sent with your next message, not with your answer.",
      ),
    ).toBeTruthy();
  });

  it("refuses a file past the per-message limit instead of sending one the server rejects", async () => {
    await renderChat();
    for (let index = 0; index < 20; index += 1) {
      await attachSpreadsheet(`sheet-${index}.csv`);
    }
    expect(uploadCount.n).toBe(20);

    await attachSpreadsheet("one-too-many.csv");

    expect(uploadCount.n).toBe(20);
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Too many files" }),
    );
  });

  it("returns a queued message's file to the tray when the workflow is missing at drain", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // The drain consumes the queued prompt before it learns there is no workflow to send to,
    // so the file must come back to the tray or it survives only on an inert bubble.
    const workflowId = saveData.workflow.workflow_id;
    saveData.workflow.workflow_id = "";
    try {
      await completeOldestStream("first done");
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "Remove queued.csv" }),
        ).toBeTruthy(),
      );
      expect(postStreaming).toHaveBeenCalledTimes(1);
    } finally {
      saveData.workflow.workflow_id = workflowId;
    }
  });

  it("asks for a message instead of silently ignoring a send with only a file", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");

    await submit("");

    expect(postStreaming).not.toHaveBeenCalled();
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Add a message" }),
    );
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();
  });

  it("sends only the files still in the tray once dictation finishes stopping", async () => {
    speechState.isListening = true;
    let finishStopping: () => void = () => {};
    speechState.stop.mockImplementationOnce(
      () =>
        new Promise<Blob | null>((resolve) => {
          finishStopping = () => resolve(null);
        }),
    );
    await renderChat();
    await attachSpreadsheet("keep.csv");
    await attachSpreadsheet("drop.csv");

    await submit("parse these");
    // The recorder is still finalizing, so the tray is still on screen and removable.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove drop.csv" }));
    });
    await act(async () => {
      finishStopping();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids).toEqual(["file_1"]);
  });

  it("deletes a sent file whose request never went out because the composer unmounted", async () => {
    const view = await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishAudioUpload: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAudioUpload = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_1",
                audio_artifact_id: "aa_1",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );

    await submit("dictated prompt");
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/chat-audio",
        expect.any(FormData),
        expect.any(Object),
      ),
    );
    // The tray is already empty and the chat request has not gone out.
    view.unmount();
    await act(async () => {
      finishAudioUpload();
    });

    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
    expect(postStreaming).not.toHaveBeenCalled();
  });

  it("returns a sent file to the tray when New chat aborts the send before it posts", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishAudioUpload: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAudioUpload = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_1",
                audio_artifact_id: "aa_1",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );

    await submit("dictated prompt");
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/chat-audio",
        expect.any(FormData),
        expect.any(Object),
      ),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      finishAudioUpload();
    });

    expect(postStreaming).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
    // Never posted, so removing it must reclaim the upload.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });
    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("refuses a send once a restored queue pushes the tray past the per-message limit", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    for (let index = 0; index < 20; index += 1) {
      await attachSpreadsheet(`sheet-${index}.csv`);
    }
    await submit("parse all twenty");
    await attachSpreadsheet("extra.csv");
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Edit queued message" }),
      );
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove sheet-0.csv" }),
      ).toBeTruthy(),
    );

    await submit("parse all twenty");

    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Too many files" }),
    );
    // Nothing left the tray, so the user can still pick which file to drop.
    expect(
      screen.getByRole("button", { name: "Remove extra.csv" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Edit queued message" }),
    ).toBeNull();
  });

  it("hands a sent file back, kept, when New chat aborts the request before the turn starts", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    await submit("parse my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream");
    await act(async () => {
      // The real streaming client resolves, not rejects, when its signal aborts.
      call.resolve();
    });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
    // The request went out, so the server may hold the file on the old chat's message.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });
    expect(deleteFile).not.toHaveBeenCalled();
  });

  it("deletes an unsent upload on page exit and drops it from a restored cached page", async () => {
    const view = await renderChat();
    await attachSpreadsheet("mine.csv");

    // A page parked in the back/forward cache may be evicted without running cleanup, so it deletes too.
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    expect(pageExitDelete).toHaveBeenCalledWith("file_1");

    // Coming back must not show a chip for an upload that no longer exists.
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Remove mine.csv" }),
      ).toBeNull(),
    );
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Attachments removed" }),
    );

    // Already reclaimed on pagehide, so unmount must not delete it a second time. The unmount
    // delete awaits its client first, so let pending work settle before checking.
    view.unmount();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(deleteFile).not.toHaveBeenCalled();
  });

  it("drops a deleted file from a queued message when a cached page is restored", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(screen.getByTitle("queued.csv")).toBeTruthy();

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    expect(pageExitDelete).toHaveBeenCalledWith("file_1");
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });

    // The queued bubble must not show, and the drain must not send, an upload that was deleted.
    await waitFor(() => expect(screen.queryByTitle("queued.csv")).toBeNull());
    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("parse the queued sheet");
    expect(streamCalls[1]?.body.attached_file_ids ?? []).toEqual([]);
  });

  it("does not delete a file on page exit while the request that carries it is still pending", async () => {
    await renderChat();
    await attachSpreadsheet("rows.csv");
    // A same-tick double submit: the first press posts, the second queues a copy holding the same id.
    await act(async () => {
      fireEvent.change(textarea(), { target: { value: "parse the sheet" } });
      const ta = textarea();
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids).toEqual(["file_1"]);

    // No turn_start yet, so the server may be resolving the file right now.
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });

    expect(pageExitDelete).not.toHaveBeenCalledWith("file_1");
  });

  it("keeps a file when its page-exit delete failed, and retries on unmount", async () => {
    const view = await renderChat();
    await attachSpreadsheet("mine.csv");
    pageExitDelete.mockResolvedValueOnce(false);

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });

    // The upload is still stored, so the chip stays and the user is told nothing was removed.
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();
    expect(vi.mocked(toast)).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Attachments removed" }),
    );

    view.unmount();
    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("deletes an upload that lands after the page was left", async () => {
    await renderChat();
    uploadGate.hold = true;
    await attachSpreadsheet("late.csv");

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    // The upload had no id when the page was left, so page-exit cleanup could not name it.
    await act(async () => {
      uploadGate.release.forEach((release) => release());
      uploadGate.release.length = 0;
    });

    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
    expect(
      screen.queryByRole("button", { name: "Remove late.csv" }),
    ).toBeNull();
  });

  it("does not send a file whose page-exit delete is still pending", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishDelete: () => void = () => {};
    pageExitDelete.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishDelete = () => resolve(true);
      }),
    );

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });

    // Restored before the delete answered: sending the id now could name a file about to go.
    await submit("parse my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids ?? []).toEqual([]);
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Attachment removed" }),
    );
    await act(async () => {
      finishDelete();
    });
  });

  it("does not drain a queued file whose page-exit delete is still pending", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    let finishDelete: () => void = () => {};
    pageExitDelete.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishDelete = () => resolve(true);
      }),
    );

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });

    // The drain carries the queued message's own files, so it has to honour the pending delete too.
    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("parse the queued sheet");
    expect(streamCalls[1]?.body.attached_file_ids ?? []).toEqual([]);
    // The bubble must not keep showing a file the request did not carry.
    expect(screen.queryByTitle("queued.csv")).toBeNull();
    await act(async () => {
      finishDelete();
    });
  });

  it("returns a file the send dropped when its page-exit delete fails", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let failDelete: () => void = () => {};
    pageExitDelete.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        failDelete = () => resolve(false);
      }),
    );

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });
    // The send drops the file and clears the tray, so nothing names the upload any more.
    await submit("parse my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids ?? []).toEqual([]);

    await act(async () => {
      failDelete();
    });

    // The upload is still stored, so its chip has to come back.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
  });

  it("puts a chip back when removing its upload fails", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    deleteFile.mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });

    // The upload is still stored, so leaving the chip removed would strand it.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
    expect(vi.mocked(toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Could not remove file" }),
    );
  });

  it("drops a file deleted while the request was still being prepared", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishAudioUpload: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAudioUpload = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_1",
                audio_artifact_id: "aa_1",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );

    await submit("parse my sheet");
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/chat-audio",
        expect.any(FormData),
        expect.any(Object),
      ),
    );
    // The file left the tray with the send, so page exit still reaches it and deletes it.
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      finishAudioUpload();
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids ?? []).toEqual([]);
  });

  it("does not hand back a file whose page-exit delete already claimed it", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishAudioUpload: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishAudioUpload = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_1",
                audio_artifact_id: "aa_1",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );
    vi.mocked(getSseClient).mockRejectedValueOnce(new Error("stream refused"));

    await submit("parse my sheet");
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/chat-audio",
        expect.any(FormData),
        expect.any(Object),
      ),
    );
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      finishAudioUpload();
    });

    // The failure path hands files back, but this one is already deleted: its chip could never be
    // removed again, because the second delete answers 404.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(
      screen.queryByRole("button", { name: "Remove mine.csv" }),
    ).toBeNull();
  });

  it("keeps a late upload whose delete failed while the page was away", async () => {
    await renderChat();
    uploadGate.hold = true;
    await attachSpreadsheet("late.csv");
    deleteFile.mockRejectedValueOnce(new Error("offline"));

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      uploadGate.release.forEach((release) => release());
      uploadGate.release.length = 0;
    });

    // The delete never landed, so the upload is still stored and needs a chip to reach it.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove late.csv" }),
      ).toBeTruthy(),
    );
  });

  it("deletes the files of every send still waiting when the composer unmounts", async () => {
    const view = await renderChat();
    await attachSpreadsheet("first.csv");
    let releaseFirstAudio: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseFirstAudio = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_1",
                audio_artifact_id: "aa_1",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );
    await submit("first message");
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/chat-audio",
        expect.any(FormData),
        expect.any(Object),
      ),
    );

    // New chat aborts that send while it is still in preflight and lets the next one start.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await attachSpreadsheet("second.csv");
    let releaseSecondAudio: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseSecondAudio = () =>
            resolve({
              data: {
                workflow_copilot_chat_id: "wcc_2",
                audio_artifact_id: "aa_2",
              },
            });
        }),
    );
    speechState.takeAudioBlob.mockReturnValueOnce(
      new Blob(["audio"], { type: "audio/webm" }),
    );
    await submit("second message");

    await waitFor(() =>
      expect(
        cancelPost.mock.calls.filter(
          ([path]) => path === "/workflow/copilot/chat-audio",
        ),
      ).toHaveLength(2),
    );

    view.unmount();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // Both sends had taken their file out of the tray, so both uploads need reclaiming.
    expect(deleteFile).toHaveBeenCalledWith("/files/file_1");
    expect(deleteFile).toHaveBeenCalledWith("/files/file_2");
    await act(async () => {
      releaseFirstAudio();
      releaseSecondAudio();
    });
  });

  it("does not start a second delete for a file already being reclaimed", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    let finishDelete: () => void = () => {};
    pageExitDelete.mockReturnValueOnce(
      new Promise<boolean>((resolve) => {
        finishDelete = () => resolve(true);
      }),
    );

    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pagehide"), { persisted: true }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        Object.assign(new Event("pageshow"), { persisted: true }),
      );
    });

    // The chip is still on screen while that delete is unresolved; removing it must not race.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(deleteFile).not.toHaveBeenCalled();
    await act(async () => {
      finishDelete();
    });
  });

  it("deletes a staged upload the user never sent when the composer unmounts", async () => {
    const view = await renderChat();
    await attachSpreadsheet("mine.csv");

    // The id lives only in component state, so leaving without sending would strand the upload.
    view.unmount();

    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("deletes an upload that finishes after the composer has unmounted", async () => {
    const view = await renderChat();
    uploadGate.hold = true;
    await attachSpreadsheet("mine.csv");

    view.unmount();
    expect(deleteFile).not.toHaveBeenCalled();

    // No chip exists to remove it from, so the completed upload must be deleted on arrival.
    await act(async () => {
      uploadGate.release.forEach((fn) => fn());
      uploadGate.release.length = 0;
    });

    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("deletes an upload the user removes from the composer before sending it", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });

    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("still deletes a returned file when the send failed before its request went out", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    vi.mocked(getSseClient).mockRejectedValueOnce(
      new Error("credential provider unavailable"),
    );

    await submit("parse my sheet");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
    expect(postStreaming).not.toHaveBeenCalled();

    // No request reached the server, so nothing else holds this upload.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });
    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("keeps an upload that was already posted when its chip is removed after a failed send", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    postStreaming.mockRejectedValueOnce(new Error("connect: refused"));
    await submit("parse my sheet");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );

    // The request went out, so the server may already hold the file on that message.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });

    expect(deleteFile).not.toHaveBeenCalled();
  });

  it("returns a replaced queued message's file to the tray", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("first.csv");
    await submit("parse the first sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // Replacing the queued message with a different file must not strand the first one.
    await attachSpreadsheet("second.csv");
    await submit("parse the second sheet instead");
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Remove first.csv" }),
    ).toBeTruthy();

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_2"]);
  });

  it("shows a queued message's attachment on its bubble", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // The tray is cleared on queue, so the bubble is the only place the file is visible.
    expect(
      screen.queryByRole("button", { name: "Remove queued.csv" }),
    ).toBeNull();
    expect(screen.getByTitle("queued.csv")).toBeTruthy();
  });

  it("returns the file to the composer, still deletable, when the server errors before a turn starts", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    await submit("parse my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // A terminal error frame ends the stream normally, so the catch-path restore never runs.
    const call = streamCalls[0];
    if (!call) throw new Error("no pending stream");
    await act(async () => {
      call.onMessage({
        type: "error",
        error: "Copilot is not configured.",
        turn_id: null,
      });
      call.resolve();
    });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
    // No turn started, so no message holds the file; removing it must not strand the upload.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove mine.csv" }));
    });
    await waitFor(() =>
      expect(deleteFile).toHaveBeenCalledWith("/files/file_1"),
    );
  });

  it("returns a queued message's file to the tray when a new chat is started", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(
      screen.queryByRole("button", { name: "Remove queued.csv" }),
    ).toBeNull();

    // Discarding the queue must not strand a file that already left the tray.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove queued.csv" }),
      ).toBeTruthy(),
    );
  });

  it("returns the file to the composer when the send fails before a turn starts", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");
    postStreaming.mockRejectedValueOnce(new Error("connect: refused"));

    await submit("parse my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // The upload succeeded and the turn never started, so the id must not be lost with the
    // failed request — the user would otherwise have to upload the same file again.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove mine.csv" }),
      ).toBeTruthy(),
    );
  });

  it("keeps the composer's file when a programmatic action is queued behind a turn", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await attachSpreadsheet("mine.csv");
    // A block regeneration requested mid-turn queues rather than sends. Queued or not, it is
    // not the user's message and must neither take the staged file nor clear it.
    await act(async () => {
      useCopilotActionStore.setState({
        pendingBuild: { blockLabel: "block_1", prompt: "make it work" },
      });
    });
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.attached_file_ids ?? []).toEqual([]);
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();
  });

  it("lets a block build send during an in-flight upload without taking the staged file", async () => {
    await renderChat();
    uploadGate.hold = true;
    await attachSpreadsheet("later.csv");

    // The upload belongs to the user's next message; a block regeneration has no stake in it,
    // so it neither waits for it nor takes it. Only a user-typed send waits for the tray.
    await act(async () => {
      useCopilotActionStore.setState({
        generatingBlockLabel: "block_1",
        pendingBuild: { blockLabel: "block_1", prompt: "make it work" },
      });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids ?? []).toEqual([]);

    await act(async () => {
      uploadGate.release.forEach((fn) => fn());
      uploadGate.release.length = 0;
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove later.csv" }),
      ).toBeTruthy(),
    );
  });

  it("keeps the composer's file when a programmatic action sends", async () => {
    await renderChat();
    await attachSpreadsheet("mine.csv");

    // A block regeneration is not the user's composed message, so it must neither carry the
    // staged file nor consume it — the user still means to send it with their own text.
    await act(async () => {
      useCopilotActionStore.setState({
        pendingBuild: { blockLabel: "block_1", prompt: "make it work" },
      });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids ?? []).toEqual([]);
    expect(
      screen.getByRole("button", { name: "Remove mine.csv" }),
    ).toBeTruthy();

    await completeOldestStream("regenerated");
    await submit("now use my sheet");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_1"]);
  });

  it("Escape returns the queued message's files to the composer, not just its text", async () => {
    await renderChat();
    await submit("first message");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    await attachSpreadsheet("queued.csv");
    await submit("parse the queued sheet");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Escape" });
    });

    // Editing the message must hand back its attachment too; otherwise the resubmit goes out
    // with no file and the user has to find and upload it again.
    expect(textarea().value).toBe("parse the queued sheet");
    expect(
      screen.getByRole("button", { name: "Remove queued.csv" }),
    ).toBeTruthy();

    // Let the in-flight turn finish, then the restored message sends with its file.
    await completeOldestStream("first done");
    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Enter" });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_1"]);
  });

  it("does not drop a queued message as a duplicate when it carries a new file", async () => {
    await renderChat();
    await submit("check these rows");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Same text as the turn in flight, but a file the turn never had — a different request.
    await attachSpreadsheet("new-rows.csv");
    await submit("check these rows");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    await completeOldestStream("first done");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expect(streamCalls[1]?.body.message).toBe("check these rows");
    expect(streamCalls[1]?.body.attached_file_ids).toEqual(["file_1"]);
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
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
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
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
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
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
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
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
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
        expect.objectContaining({
          timeout: 15_000,
          signal: expect.any(AbortSignal),
        }),
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
        expect.objectContaining({
          timeout: 15_000,
          signal: expect.any(AbortSignal),
        }),
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
        expect.objectContaining({
          timeout: 15_000,
          signal: expect.any(AbortSignal),
        }),
      );

      await deliverFirstFrame();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.anything(),
        expect.objectContaining({
          timeout: 15_000,
          signal: expect.any(AbortSignal),
        }),
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
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
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
    expect(screen.getByRole("alert").textContent).toContain(
      "Your draft is retained",
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
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
      { timeout: 30_000, signal: expect.any(AbortSignal) },
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

  it("drops a same-tick repeat of a message that carried a file", async () => {
    await renderChat();
    await attachSpreadsheet("rows.csv");

    // Two Enter presses before React commits: the second still sees the same text and tray, so it
    // queues an exact copy of the request the first one just sent.
    await act(async () => {
      fireEvent.change(textarea(), { target: { value: "parse the sheet" } });
      const ta = textarea();
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.attached_file_ids).toEqual(["file_1"]);

    await completeOldestStream("first done");
    await act(async () => {});

    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("keeps a started turn's file when its queued duplicate is edited and the file removed", async () => {
    await renderChat();
    await attachSpreadsheet("rows.csv");
    await act(async () => {
      fireEvent.change(textarea(), { target: { value: "parse the sheet" } });
      const ta = textarea();
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
      ta.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await deliverFirstFrame();

    // Editing the duplicate hands its ids back to the tray while the first turn still uses them.
    await act(async () => {
      fireEvent.keyDown(textarea(), { key: "Escape" });
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove rows.csv" }),
      ).toBeTruthy(),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove rows.csv" }));
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(deleteFile).not.toHaveBeenCalled();
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
          turnFacts: {
            factsAvailable: true,
            evaluationState: "not_demonstrated",
            runId: null,
            runCompleted: true,
            terminalCause: null,
            blocksRunThisTurn: 1,
            ranCleanOnCurrentSource: false,
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
