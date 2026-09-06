import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";

type StreamBody = { message: string; workflow_run_id?: string | null };
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const {
  streamCalls,
  postStreaming,
  cancelPost,
  historyResponse,
  routeParams,
  timelineGet,
} = vi.hoisted(() => {
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
  const params = {
    current: {
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined as string | undefined,
    },
  };
  const timeline = vi.fn().mockResolvedValue({ data: [] });
  return {
    streamCalls: calls,
    postStreaming: streaming,
    cancelPost: post,
    historyResponse: history,
    routeParams: params,
    timelineGet: timeline,
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes("/timeline")) return timelineGet(url);
      return Promise.resolve(historyResponse);
    }),
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const { switchStudioRun, releaseStudioRun } = vi.hoisted(() => ({
  switchStudioRun: vi.fn(),
  releaseStudioRun: vi.fn(),
}));

vi.mock("@/routes/workflows/studio/runSwitchNavigation", () => ({
  useSwitchStudioRun: () => switchStudioRun,
  useReleaseStudioRun: () => releaseStudioRun,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => routeParams.current,
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

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
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

import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { useRecordingStore } from "@/store/useRecordingStore";

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = { docked?: boolean; portalTarget?: HTMLElement | null };

function chatUi(props: ChatProps = {}) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <WorkflowCopilotChat {...props} />
    </FeatureFlagContext.Provider>
  );
}

// A docked chat portals its content, rendering null without a body-attached target.
function makeDockedProps(): ChatProps {
  const portalTarget = document.createElement("div");
  document.body.appendChild(portalTarget);
  return { docked: true, portalTarget };
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

async function submit(value: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value } });
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
}

const runOutcomeFrame = (overrides: Partial<Record<string, unknown>> = {}) => ({
  type: "run_outcome",
  workflow_run_id: "wr_1",
  workflow_run_block_ids: ["wrb_1"],
  block_labels: ["block_1"],
  verdict: "evaluating",
  iteration: 0,
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

const blockProgressFrame = (
  overrides: Partial<Record<string, unknown>> = {},
) => ({
  type: "block_progress",
  workflow_run_block_id: "wrb_1",
  block_label: "block_1",
  block_type: "code",
  status: "running",
  iteration: 0,
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

const runStartedFrame = (overrides: Partial<Record<string, unknown>> = {}) => ({
  type: "run_started",
  workflow_run_id: "wr_1",
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

beforeEach(() => {
  switchStudioRun.mockClear();
  releaseStudioRun.mockClear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  timelineGet.mockClear();
  timelineGet.mockResolvedValue({ data: [] });
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
  routeParams.current = {
    workflowPermanentId: "wpid_1",
    workflowRunId: undefined,
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — recorded-action live poll wiring", () => {
  it("starts a live poll from the first block_progress that carries a run id", async () => {
    await renderChat();
    await submit("build a workflow");

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
    streamCalls[0]!.onMessage(
      blockProgressFrame({ workflow_run_id: "wr_1", status: "running" }),
    );

    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
  });

  it("polls the timeline repeatedly while the run stays live", async () => {
    // Call through so React/testing-library timers keep working; we only need
    // to capture the registered callback to drive it deterministically.
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ workflow_run_id: "wr_1" }),
      );
      // Immediate fetch on the first sighting.
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 2500);

      // Drive the registered interval callback: each tick re-fetches (the
      // reducer merges by actionId, so repeated fetches never duplicate rows).
      const tick = setIntervalSpy.mock.calls.find((c) => c[1] === 2500)![0] as (
        ...args: unknown[]
      ) => void;
      tick();
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));
      tick();
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(3));
    } finally {
      setIntervalSpy.mockRestore();
    }
  });

  it("converges with a final fetch and stops polling on a terminal run_outcome", async () => {
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ workflow_run_id: "wr_1" }),
      );
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      const idx = setIntervalSpy.mock.calls.findIndex((c) => c[1] === 2500);
      const intervalId = setIntervalSpy.mock.results[idx]!.value;

      // Terminal verdict: one convergent fetch, then the interval is cleared.
      streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));
      expect(clearIntervalSpy).toHaveBeenCalledWith(intervalId);
    } finally {
      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    }
  });

  it("falls back to run_outcome(evaluating) when block_progress carries no run id", async () => {
    await renderChat();
    await submit("build a workflow");

    // Old backend: block_progress has no run id, so no poll can start yet.
    streamCalls[0]!.onMessage(blockProgressFrame({ status: "running" }));
    streamCalls[0]!.onMessage(blockProgressFrame({ status: "completed" }));
    expect(timelineGet).not.toHaveBeenCalled();

    // The run id first arrives on run_outcome — the poll starts there.
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "evaluating" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
  });

  it("never fetches when frames carry an empty workflow_run_id", async () => {
    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage(blockProgressFrame({ workflow_run_id: "" }));
    streamCalls[0]!.onMessage(runOutcomeFrame({ workflow_run_id: "" }));

    expect(timelineGet).not.toHaveBeenCalled();
  });

  it("renders the recorded actions once the timeline fetch resolves", async () => {
    timelineGet.mockResolvedValue({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_1",
            actions: [
              {
                action_id: "a1",
                action_type: "wobble_gizmo",
                status: "completed",
                task_id: null,
                step_id: null,
                step_order: null,
                action_order: 0,
                confidence_float: null,
                description: null,
                reasoning: "Wobbled the gizmo into place",
                intention: null,
                response: null,
                created_by: null,
                text: null,
                output: { duration_ms: 220 },
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });

    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "running",
      iteration: 0,
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "completed",
      iteration: 0,
      timestamp: "2026-06-10T00:00:05Z",
    });
    streamCalls[0]!.onMessage(runOutcomeFrame());

    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("Wobble Gizmo")).toBeTruthy());
  });

  it("patches an already-frozen AI message when the timeline fetch resolves after the terminal response", async () => {
    let resolveTimeline!: (value: { data: unknown[] }) => void;
    timelineGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTimeline = resolve;
        }),
    );

    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "running",
      iteration: 0,
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "completed",
      iteration: 0,
      timestamp: "2026-06-10T00:00:05Z",
    });
    streamCalls[0]!.onMessage(runOutcomeFrame());
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));

    // Terminal response freezes the narrative BEFORE the timeline fetch
    // (started above) resolves.
    streamCalls[0]!.onMessage({
      type: "response",
      workflow_copilot_chat_id: "chat_1",
      message: "Done",
      response_time: "2026-06-10T00:00:06Z",
      proposal_disposition: "no_proposal",
      turn_id: "turn-1",
      narrative_payload: null,
    });
    // The bottom live bubble and the newly-frozen message both briefly carry
    // role="status"; wait for the live one to unmount (terminal narrative)
    // rather than grabbing whichever settles first.
    await waitFor(() => {
      expect(screen.getAllByRole("status")).toHaveLength(1);
    });

    resolveTimeline({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_1",
            actions: [
              {
                action_id: "a1",
                action_type: "wobble_gizmo",
                status: "completed",
                task_id: null,
                step_id: null,
                step_order: null,
                action_order: 0,
                confidence_float: null,
                description: null,
                reasoning: "Wobbled the gizmo into place",
                intention: null,
                response: null,
                created_by: null,
                text: null,
                output: { duration_ms: 220 },
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });

    // The current settled turn remains expanded, so expand the block row to
    // reach the replay the fetch just patched in — this is the reviewer's
    // "does the verify card ever receive it".
    const statusRegion = await waitFor(() => screen.getByRole("status"));
    // The row's primary text is the humanized label ("block_1" -> "Block 1"),
    // not the raw block label.
    fireEvent.click(within(statusRegion).getByText("Block 1"));

    await waitFor(() => expect(screen.getByText("Wobble Gizmo")).toBeTruthy());
  });
});

describe("WorkflowCopilotChat — studio run focus", () => {
  it("focuses the dispatched run from run_started and does not re-navigate on block_progress", async () => {
    await renderChat(makeDockedProps());
    await submit("build a workflow");

    streamCalls[0]!.onMessage(runStartedFrame());
    await waitFor(() => expect(switchStudioRun).toHaveBeenCalledTimes(1));
    expect(switchStudioRun).toHaveBeenCalledWith("wr_1");

    streamCalls[0]!.onMessage(blockProgressFrame({ workflow_run_id: "wr_1" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(switchStudioRun).toHaveBeenCalledTimes(1);
  });
});

describe("WorkflowCopilotChat — build follow", () => {
  const focusBlock = vi.fn();

  beforeEach(() => {
    focusBlock.mockClear();
    useWorkflowBlockSearchStore.getState().registerHandle({
      getTargets: () => [
        { nodeId: "node_login", label: "login", blockType: "task" },
      ],
      focusBlock,
    });
    window.history.pushState(null, "", "/?panes=copilot,editor");
  });

  afterEach(() => {
    useWorkflowBlockSearchStore.getState().registerHandle(null);
    window.history.pushState(null, "", "/");
  });

  it("focuses the canvas on the block a progress frame names", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).toHaveBeenCalledWith("node_login");
  });

  it("follows a block only once per label", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );
    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "completed" }),
    );

    expect(focusBlock).toHaveBeenCalledTimes(1);
  });

  it("stops following after the user presses outside the copilot pane", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    fireEvent.pointerDown(document.body);
    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).not.toHaveBeenCalled();
  });

  it("does not follow when the editor pane is closed", async () => {
    window.history.pushState(null, "", "/?panes=copilot");
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).not.toHaveBeenCalled();
  });

  it("never fights the recording overlay", async () => {
    useRecordingStore.setState({ isRecording: true });
    try {
      await renderChat(makeDockedProps());
      await submit("build it");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ block_label: "login", status: "running" }),
      );

      expect(focusBlock).not.toHaveBeenCalled();
    } finally {
      useRecordingStore.setState({ isRecording: false });
    }
  });
});
