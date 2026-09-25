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
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
} from "./WorkflowCopilotChat";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import { COPILOT_WORKING_VERBS } from "./workingVerbs";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";

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

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => {
    throw new Error("WorkflowCopilotChat must not consult PostHog");
  },
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
const recordingPanelMounts = vi.hoisted(() => ({ nextId: 0 }));

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
  const state = { getSaveData: () => saveData, setSaveBlockedReason: () => {} };
  return {
    useWorkflowHasChangesStore: Object.assign(() => state, {
      getState: () => state,
    }),
  };
});

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

vi.mock("@/routes/workflows/editor/recording/RecordingPanel", async () => {
  const { forwardRef, useRef } = await import("react");
  const { createPortal } = await import("react-dom");
  return {
    RecordingPanel: forwardRef<
      HTMLDivElement,
      {
        expanded?: boolean;
        onExpandedChange?: (expanded: boolean) => void;
        onBackToChat?: () => void;
        suggestionPortalTarget?: HTMLElement | null;
      }
    >(function MockRecordingPanel(
      { expanded, onExpandedChange, onBackToChat, suggestionPortalTarget },
      ref,
    ) {
      const mountId = useRef(++recordingPanelMounts.nextId).current;
      return (
        <>
          <div
            ref={ref}
            data-mount-id={mountId}
            data-recording-controller="true"
            data-testid={expanded ? "recording-focus" : "recording-inline"}
          >
            {expanded ? (
              <button type="button" onClick={onBackToChat}>
                Back to chat
              </button>
            ) : (
              <button type="button" onClick={() => onExpandedChange?.(true)}>
                Expand recording
              </button>
            )}
          </div>
          {suggestionPortalTarget
            ? createPortal(
                <div data-testid="mock-recording-suggestion">Suggestion</div>,
                suggestionPortalTarget,
              )
            : null}
        </>
      );
    }),
  };
});

type FlagConfig = {
  codeBlockMode?: boolean;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
  liveBrowserSessionId?: string;
  isOpen?: boolean;
};

async function renderChat(flags: FlagConfig) {
  const booleanFlags: Record<string, boolean> = {
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: flags.codeBlockMode ?? false,
    CODE_BLOCK_ACCESS: flags.codeBlockMode ?? false,
  };
  const view = render(
    <FeatureFlagContext.Provider value={(name) => booleanFlags[name]}>
      <WorkflowCopilotChat
        isOpen={flags.isOpen}
        requiresLiveBrowser={flags.requiresLiveBrowser}
        isLiveBrowserReady={flags.isLiveBrowserReady}
        liveBrowserSessionId={flags.liveBrowserSessionId}
      />
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

async function deliverFirstFrame() {
  const call = streamCalls[streamCalls.length - 1];
  if (!call) throw new Error("no pending stream to open");
  await act(async () => {
    call.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-05-25T00:00:00Z",
    });
  });
}

beforeEach(() => {
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  recordingPanelMounts.nextId = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  useRecordingStore.setState({
    isRecording: false,
    finishRequested: false,
    isCommitting: false,
  });
  useCopilotActionStore.setState({
    pendingBuild: null,
    generatingBlockLabel: null,
    cancelNonce: 0,
  });
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  useRecordingStore.setState({
    isRecording: false,
    finishRequested: false,
    isCommitting: false,
  });
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

describe("WorkflowCopilotChat — unflagged S4 composer", () => {
  it("morphs to stop while running with an empty box, and cancels the run on click", async () => {
    await renderChat({ codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    // Sent but not yet streaming: the control stays pressable, and the cancel
    // chokepoint - not a disabled attribute - is what issues nothing.
    const pending = screen.getByRole("button", { name: "Starting…" });
    expect(pending.hasAttribute("disabled")).toBe(false);
    expect(pending.getAttribute("aria-busy")).not.toBe("true");
    await act(async () => {
      fireEvent.click(pending);
    });
    expect(cancelPost).not.toHaveBeenCalled();
    expect(postStreaming).toHaveBeenCalledTimes(1);
    await deliverFirstFrame();

    await act(async () => {
      useRecordingStore.setState({ isRecording: true });
    });
    const button = screen.getByRole("button", { name: "Stop" });
    expect(screen.getByTestId("copilot-stop-orbit").className).not.toContain(
      "paused",
    );
    expect((button as HTMLButtonElement).disabled).toBe(false);

    await act(async () => {
      fireEvent.click(button);
    });
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("copilot-stop-orbit").className).toContain(
      "paused",
    );
    // Cancelling must stay legible without motion: the button also goes
    // disabled, which is what dims it under prefers-reduced-motion.
    const stopping = screen.getByRole("button", { name: "Stopping…" });
    expect((stopping as HTMLButtonElement).disabled).toBe(true);
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
    );
    useRecordingStore.setState({ isRecording: false });
  });

  it("arms stop on the first streamed frame even when that frame carries no turn id", async () => {
    await renderChat({ codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const call = streamCalls[streamCalls.length - 1];
    if (!call) throw new Error("no pending stream to open");
    await act(async () => {
      call.onMessage({ type: "condensing" });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    });
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
  });

  it("re-arms stop once a turn has streamed nothing for the arming window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await renderChat({ codeBlockMode: true });
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      expect(screen.getByRole("button", { name: "Starting…" })).toBeTruthy();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Stop" }));
      });
      await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels a block-level stop before the first frame, when Stop itself would not", async () => {
    // The arming gate exists so a fast double-click on Send is not "send, then cancel".
    // A block's own cancel is a deliberate gesture, so it must not go dead during that
    // window — a turn hanging before its first frame is when someone reaches for it.
    await renderChat({ codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await act(async () => {
      useCopilotActionStore.getState().requestCancel();
    });
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.objectContaining({ source: "stop_button" }),
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("hands a queued prompt back to the composer rather than letting the stop auto-fire it", async () => {
    await renderChat({ codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea(), {
      target: { value: "also grab the story scores" },
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Queue for next turn" }),
      );
    });
    expect(textarea().value).toBe("");

    await act(async () => {
      useCopilotActionStore.getState().requestCancel();
    });

    // The stop lands, and the queued follow-up is handed back rather than auto-firing.
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    expect(postStreaming).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(textarea().value).toBe("also grab the story scores"),
    );
  });

  it("flips back to a queueing send when typing mid-run, and queues on click", async () => {
    await renderChat({ codeBlockMode: true });
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
    // The queue now rides in the working row's pill; the standalone chip and
    // the legacy prose status line are both gone, so the state is stated once.
    expect(screen.getByText(/1 message queued/)).toBeTruthy();
    expect(screen.queryByText("Queued")).toBeNull();
    expect(
      screen.queryByText("Queued — sends when this turn finishes."),
    ).toBeNull();
  });

  it("shows a cycling Skyvern verb instead of the prose working line", async () => {
    await renderChat({ codeBlockMode: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    const row = screen.getByTestId("copilot-working-status");
    expect(
      COPILOT_WORKING_VERBS.some((verb) =>
        row.textContent?.includes(`${verb}…`),
      ),
    ).toBe(true);
    expect(
      screen.queryByText(
        "Copilot is working. Your next send will wait for the next turn.",
      ),
    ).toBeNull();
  });

  it("disables the morph button (not a dead-looking Send) while a prompt waits on the live browser", async () => {
    await renderChat({
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
      name: "Edit queued message",
    });
    await act(async () => {
      fireEvent.click(cancel);
    });
    expect(screen.queryByText("Queued")).toBeNull();
    expect(
      screen.queryByText("Prompt queued. Waiting for live browser..."),
    ).toBeNull();
  });

  it("embeds the input: idle placeholder matches the mock, textarea borderless inside a focus-within container", async () => {
    await renderChat({ codeBlockMode: true });
    const ta = textarea();

    expect(ta.getAttribute("placeholder")).toBe(
      "Ask Copilot to build or change your workflow…",
    );
    // Border + focus ring moved off the textarea onto the wrapping container.
    expect(ta.className).not.toContain("border-input");
    expect(ta.parentElement?.className).toContain("focus-within");
  });

  it("starts SOP upload and task recording from the empty state", async () => {
    const onUploadSOP = vi.fn();
    const onRecordTask = vi.fn();

    render(
      <FeatureFlagContext.Provider value={() => false}>
        <WorkflowCopilotChat
          onUploadSOP={onUploadSOP}
          onRecordTask={onRecordTask}
          canRecordTask
        />
      </FeatureFlagContext.Provider>,
    );

    await waitFor(() =>
      expect(screen.getByText("Start a new chat")).toBeTruthy(),
    );
    expect(screen.getByText("Upload an SOP")).toBeTruthy();
    expect(screen.getByText("Record task")).toBeTruthy();
    const actionLayout = screen.getByRole("button", {
      name: "Upload an SOP",
    }).parentElement?.parentElement;
    expect(actionLayout?.className).toContain("grid-cols-1");
    expect(actionLayout?.className).toContain(
      "[@container_copilot-actions_(min-width:440px)]:grid-cols-2",
    );
    expect(
      screen.getByText(
        "Complete the task in the browser. Skyvern captures the browser view and your clicks, typing, and navigation, then turns them into workflow steps.",
      ),
    ).toBeTruthy();

    const file = new File(["procedure"], "procedure.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText("Choose an SOP PDF"), {
      target: { files: [file] },
    });
    expect(onUploadSOP).toHaveBeenCalledWith(file);

    fireEvent.click(screen.getByRole("button", { name: "Record task" }));
    expect(onRecordTask).toHaveBeenCalledTimes(1);
  });

  it("does not allow SOP upload and task recording at the same time", async () => {
    const onUploadSOP = vi.fn();
    const onRecordTask = vi.fn();
    const { rerender } = render(
      <FeatureFlagContext.Provider value={() => false}>
        <WorkflowCopilotChat
          onUploadSOP={onUploadSOP}
          canUploadSOP
          onRecordTask={onRecordTask}
          canRecordTask
          isUploadingSOP
        />
      </FeatureFlagContext.Provider>,
    );

    await waitFor(() =>
      expect(screen.getByText("Uploading SOP…")).toBeTruthy(),
    );
    const recordTaskButton = screen.getByRole("button", {
      name: "Record task",
    });
    expect(recordTaskButton.hasAttribute("disabled")).toBe(true);
    expect(recordTaskButton.parentElement?.getAttribute("tabindex")).toBe("0");
    expect((textarea() as HTMLTextAreaElement).disabled).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "Send disabled — finish the current authoring action",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);

    rerender(
      <FeatureFlagContext.Provider value={() => false}>
        <WorkflowCopilotChat
          onUploadSOP={onUploadSOP}
          canUploadSOP={false}
          onRecordTask={onRecordTask}
          canRecordTask
        />
      </FeatureFlagContext.Provider>,
    );

    const uploadSOPButton = screen.getByRole("button", {
      name: "Upload an SOP",
    });
    expect(uploadSOPButton.hasAttribute("disabled")).toBe(true);
    expect(uploadSOPButton.parentElement?.getAttribute("tabindex")).toBe("0");
  });

  it.each(["yaml", "copilot"])(
    "refuses authoring launches when a %s transaction starts after the file picker opens",
    async (owner) => {
      const onUploadSOP = vi.fn();
      const onRecordTask = vi.fn();
      render(
        <FeatureFlagContext.Provider value={() => false}>
          <WorkflowCopilotChat
            onUploadSOP={onUploadSOP}
            onRecordTask={onRecordTask}
            canRecordTask
          />
        </FeatureFlagContext.Provider>,
      );
      await waitFor(() =>
        expect(screen.getByText("Start a new chat")).toBeTruthy(),
      );
      fireEvent.click(screen.getByRole("button", { name: "Upload an SOP" }));
      act(() => {
        useWorkflowYamlEditorStore.setState({
          commitInProgress: owner === "yaml",
          copilotAcceptance: owner === "copilot" ? Symbol("turn") : null,
        });
      });
      fireEvent.change(screen.getByLabelText("Choose an SOP PDF"), {
        target: {
          files: [
            new File(["procedure"], "procedure.pdf", {
              type: "application/pdf",
            }),
          ],
        },
      });
      fireEvent.click(screen.getByRole("button", { name: "Record task" }));
      expect(onUploadSOP).not.toHaveBeenCalled();
      expect(onRecordTask).not.toHaveBeenCalled();
    },
  );

  it("keeps the initial prompt pending until authoring output has reached the editor", async () => {
    useWorkflowYamlEditorStore.setState({ authoringInProgress: true });
    const onInitialMessageConsumed = vi.fn();
    render(
      <FeatureFlagContext.Provider value={() => false}>
        <WorkflowCopilotChat
          initialMessage="Refine the captured workflow"
          onInitialMessageConsumed={onInitialMessageConsumed}
        />
      </FeatureFlagContext.Provider>,
    );
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "New chat" })
          .hasAttribute("disabled"),
      ).toBe(false),
    );
    expect(screen.getAllByText("Refine the captured workflow")).toHaveLength(1);
    expect(onInitialMessageConsumed).not.toHaveBeenCalled();
    expect(streamCalls).toHaveLength(0);

    act(() =>
      useWorkflowYamlEditorStore.setState({ authoringInProgress: false }),
    );
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0]?.body.message).toBe("Refine the captured workflow");
    expect(onInitialMessageConsumed).toHaveBeenCalledOnce();
    expect(screen.getAllByText("Refine the captured workflow")).toHaveLength(1);
  });

  it("places the mic and send inside the container, with the mic to the right of the input", async () => {
    await renderChat({ codeBlockMode: true });
    const ta = textarea();
    const container = ta.parentElement as HTMLElement;
    const mic = screen.getByRole("button", { name: "Dictate message" });
    const send = screen.getByRole("button", { name: "Send" });

    expect(container.contains(mic)).toBe(true);
    expect(container.contains(send)).toBe(true);
    // Embedded on the right: the mic now follows the textarea in DOM order
    // (it sits before it in the legacy flat row).
    expect(
      ta.compareDocumentPosition(mic) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps the credential suggestion below the latest Copilot reply while recording", async () => {
    await renderChat({
      codeBlockMode: true,
      liveBrowserSessionId: "pbs_recording",
    });
    await act(async () => {
      useRecordingStore.setState({ isRecording: true });
    });

    await submit("Always choose the first available appointment.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "response",
        message: "Got it — I’ll keep that preference with this demonstration.",
        workflow_copilot_chat_id: "chat-1",
        response_time: "2026-09-19T20:00:00Z",
        proposal_disposition: "no_proposal",
      });
      streamCalls[0]?.resolve();
    });

    const copilotReply = screen.getByText(
      "Got it — I’ll keep that preference with this demonstration.",
    );
    const suggestion = screen.getByTestId("mock-recording-suggestion");
    expect(
      screen.getByTestId("recording-suggestion-host").contains(suggestion),
    ).toBe(true);
    expect(
      copilotReply.compareDocumentPosition(suggestion) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      document.querySelectorAll('[data-recording-controller="true"]'),
    ).toHaveLength(1);
  });

  it("returns from full-pane recording to the transcript when the user sends", async () => {
    useRecordingStore.setState({ isRecording: true });
    await renderChat({
      codeBlockMode: true,
      liveBrowserSessionId: "pbs_recording",
    });
    const mountId = screen
      .getByTestId("recording-inline")
      .getAttribute("data-mount-id");

    fireEvent.click(screen.getByRole("button", { name: "Expand recording" }));
    expect(screen.getByTestId("recording-focus")).toBeTruthy();
    expect(
      document.querySelectorAll('[data-recording-controller="true"]'),
    ).toHaveLength(1);

    await submit("Only download invoices marked Paid.");

    expect(screen.queryByTestId("recording-focus")).toBeNull();
    expect(screen.getByTestId("recording-inline")).toBeTruthy();
    expect(
      screen.getByTestId("recording-inline").getAttribute("data-mount-id"),
    ).toBe(mountId);
    expect(
      document.querySelectorAll('[data-recording-controller="true"]'),
    ).toHaveLength(1);
    expect(
      screen.getByText("Only download invoices marked Paid."),
    ).toBeTruthy();
  });

  it("keeps the recording controller mounted while the Copilot pane is closed", async () => {
    useRecordingStore.setState({
      isRecording: false,
      finishRequested: true,
    });
    const { rerender } = await renderChat({
      codeBlockMode: true,
      liveBrowserSessionId: "pbs_recording",
      isOpen: false,
    });

    expect(screen.getByTestId("recording-inline")).toBeTruthy();
    const mountId = screen
      .getByTestId("recording-inline")
      .getAttribute("data-mount-id");
    expect(
      document.querySelectorAll('[data-recording-controller="true"]'),
    ).toHaveLength(1);

    rerender(
      <FeatureFlagContext.Provider value={() => true}>
        <WorkflowCopilotChat isOpen liveBrowserSessionId="pbs_recording" />
      </FeatureFlagContext.Provider>,
    );

    expect(screen.getByTestId("recording-inline")).toBeTruthy();
    // A finishing recording refuses sends, so the composer must not offer one.
    expect(textarea().disabled).toBe(true);
    expect(
      screen.getByTestId("recording-inline").getAttribute("data-mount-id"),
    ).toBe(mountId);
    expect(
      document.querySelectorAll('[data-recording-controller="true"]'),
    ).toHaveLength(1);
  });

  it("keeps the recording controller mounted through a browser session id blip", async () => {
    useRecordingStore.setState({
      isRecording: false,
      finishRequested: true,
    });
    const { rerender } = await renderChat({
      codeBlockMode: true,
      liveBrowserSessionId: "pbs_recording",
    });
    const mountId = screen
      .getByTestId("recording-inline")
      .getAttribute("data-mount-id");

    rerender(
      <FeatureFlagContext.Provider value={() => true}>
        <WorkflowCopilotChat />
      </FeatureFlagContext.Provider>,
    );
    expect(screen.getByTestId("recording-inline")).toBeTruthy();
    expect(
      screen.getByTestId("recording-inline").getAttribute("data-mount-id"),
    ).toBe(mountId);

    rerender(
      <FeatureFlagContext.Provider value={() => true}>
        <WorkflowCopilotChat liveBrowserSessionId="pbs_recording" />
      </FeatureFlagContext.Provider>,
    );
    expect(
      screen.getByTestId("recording-inline").getAttribute("data-mount-id"),
    ).toBe(mountId);
  });
});
