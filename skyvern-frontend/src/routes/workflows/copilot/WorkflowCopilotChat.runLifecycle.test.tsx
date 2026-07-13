import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import {
  FeatureFlagContext,
  FeatureFlagValueContext,
} from "@/hooks/useFeatureFlag";

type StreamBody = {
  message: string;
  workflow_run_id?: string | null;
  fix_origin?: boolean;
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
  cancelPost,
  historyResponse,
  routeParams,
  workflowRunQueryMock,
  mockFeatureFlagEnabled,
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
  return {
    streamCalls: calls,
    postStreaming: streaming,
    cancelPost: post,
    historyResponse: history,
    routeParams: params,
    workflowRunQueryMock: vi.fn(),
    mockFeatureFlagEnabled: vi.fn().mockReturnValue(true),
  };
});

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: mockFeatureFlagEnabled,
}));

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
    useParams: () => routeParams.current,
    useSearchParams: () => [new URLSearchParams(), vi.fn()],
  };
});

vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: (options?: { workflowRunId?: string }) =>
    workflowRunQueryMock(options),
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

// Real WorkflowCopilotHistory needs an infinite-query + debounced Popover;
// a button standing in for "pick a different past chat" is enough here.
vi.mock("./WorkflowCopilotHistory", () => ({
  WorkflowCopilotHistory: ({
    onSelect,
  }: {
    onSelect: (chat: { workflow_copilot_chat_id: string }) => void;
  }) => (
    <button
      onClick={() => onSelect({ workflow_copilot_chat_id: "chat_other" })}
    >
      mock-select-history-chat
    </button>
  ),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  ENABLE_WORKFLOW_COPILOT_V2: true,
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  workflowRunId?: string | null;
  docked?: boolean;
  portalTarget?: HTMLElement | null;
};

function chatUi(props: ChatProps) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat {...props} />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>
  );
}

// docked renders via createPortal(content, portalTarget) and is null without
// one, so docked tests need a real, body-attached portal target to query into.
function makeDockedProps(props: ChatProps = {}): ChatProps {
  const portalTarget = document.createElement("div");
  document.body.appendChild(portalTarget);
  return { docked: true, portalTarget, ...props };
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/Message Skyvern Copilot/)).toBeTruthy(),
  );
  return view;
}

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    workflow_run_id: "wr_1",
    status: Status.Running,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: "2026-01-01T00:00:10Z",
    outputs: null,
    failure_reason: null,
    ...overrides,
  };
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
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
  workflowRunQueryMock.mockReset();
  workflowRunQueryMock.mockReturnValue({ data: undefined });
  mockFeatureFlagEnabled.mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — run lifecycle lines", () => {
  it("REGRESSION: a docked chat renders a lifecycle line when its focused run transitions running -> completed", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const props = makeDockedProps({ workflowRunId: "wr_1" });
    const view = await renderChat(props);

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:42Z",
      }),
    });
    view.rerender(chatUi(props));

    await waitFor(() =>
      expect(screen.getByText(/Run completed in 0:42\./)).toBeTruthy(),
    );
  });

  it("keeps proposal actions attached to the last real ai message when a lifecycle entry trails it", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat_1",
      chat_history: [
        {
          sender: "user",
          content: "build something",
          created_at: "2026-01-01T00:00:00Z",
        },
        {
          sender: "ai",
          content: "Here's a draft.",
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
      proposed_workflow: saveData,
      auto_accept: false,
    };
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const props = makeDockedProps({ workflowRunId: "wr_1" });
    const view = await renderChat(props);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ status: Status.Completed }),
    });
    view.rerender(chatUi(props));

    await waitFor(() =>
      expect(screen.getByText(/Run completed in/)).toBeTruthy(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("renders no lifecycle UI for a non-docked (legacy) chat even with a route-level run id and live data, and never queries it", async () => {
    routeParams.current = {
      workflowPermanentId: "wpid_1",
      workflowRunId: "wr_route",
    };
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ workflow_run_id: "wr_route" }),
    });
    await renderChat({ docked: false });

    // Give any (incorrect) announcement a chance to land before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/watching it now/)).toBeNull();

    // REGRESSION: a plain omitted workflowRunId used to still let
    // useWorkflowRunQuery fall back to the route's own :workflowRunId and poll it.
    expect(workflowRunQueryMock.mock.calls.length).toBeGreaterThan(0);
    for (const call of workflowRunQueryMock.mock.calls) {
      expect(call[0]).toEqual({ workflowRunId: undefined, enabled: false });
    }
  });

  it("renders no lifecycle line when copilot_ux_v1 is off, even for a docked chat with a live run", async () => {
    mockFeatureFlagEnabled.mockReturnValue(false);
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const props = makeDockedProps({ workflowRunId: "wr_1" });
    const view = await renderChat(props);

    // Give any (incorrect) announcement a chance to land before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/watching it now/)).toBeNull();

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:42Z",
      }),
    });
    view.rerender(chatUi(props));

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/Run completed in/)).toBeNull();

    // Flag off must genuinely disable the run query, not just hide its output —
    // otherwise a docked-off/flag-off chat still polls the run in the background.
    expect(workflowRunQueryMock.mock.calls.length).toBeGreaterThan(0);
    for (const call of workflowRunQueryMock.mock.calls) {
      expect(call[0]).toEqual({ workflowRunId: undefined, enabled: false });
    }
  });

  it("New chat wipes a rendered lifecycle line", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    await renderChat(makeDockedProps({ workflowRunId: "wr_1" }));

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole("button", { name: /New chat/i }));

    expect(screen.queryByText(/watching it now/)).toBeNull();
  });

  it("REGRESSION: switching to a different past chat drops the lifecycle line instead of carrying it forward", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    await renderChat(makeDockedProps({ workflowRunId: "wr_1" }));

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    historyResponse.data = {
      workflow_copilot_chat_id: "chat_other",
      chat_history: [
        {
          sender: "ai",
          content: "An old unrelated reply.",
          created_at: "2025-01-01T00:00:00Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    fireEvent.click(
      screen.getByRole("button", { name: "mock-select-history-chat" }),
    );

    await waitFor(() =>
      expect(screen.getByText("An old unrelated reply.")).toBeTruthy(),
    );
    expect(screen.queryByText(/watching it now/)).toBeNull();
  });
});
