import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

const { streamCalls, postStreaming, historyGet, historyResponse } = vi.hoisted(
  () => {
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
        workflow_copilot_chat_id: null as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null as Record<string, unknown> | null,
        auto_accept: false,
        work_plan: [] as string[],
      },
    };
    const get = vi.fn().mockImplementation(() => Promise.resolve(history));
    return {
      streamCalls: calls,
      postStreaming: streaming,
      historyGet: get,
      historyResponse: history,
    };
  },
);

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: historyGet,
    post: vi.fn().mockResolvedValue({}),
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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat() {
  const view = render(
    <FeatureFlagContext.Provider value={() => false}>
      <WorkflowCopilotChat />
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

async function submitTurn(value: string) {
  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  fireEvent.change(textarea, { target: { value } });
  await act(async () => {
    fireEvent.keyDown(textarea, { key: "Enter" });
  });
  await waitFor(() => expect(postStreaming).toHaveBeenCalled());
}

function terminalResponse(workPlan: string[] | null) {
  return {
    type: "response" as const,
    workflow_copilot_chat_id: "chat-1",
    message: "Saved a draft.",
    updated_workflow: null,
    response_time: "2026-09-04T00:00:05Z",
    proposal_disposition: "no_proposal" as const,
    work_plan: workPlan,
  };
}

async function finishTurn(index: number, workPlan: string[] | null) {
  await act(async () => {
    streamCalls[index]!.onMessage(terminalResponse(workPlan));
    streamCalls[index]!.resolve();
  });
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  historyGet.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
    work_plan: [],
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — work plan liveness", () => {
  it("renders the stored plan the session history returns at mount", async () => {
    historyResponse.data = {
      ...historyResponse.data,
      workflow_copilot_chat_id: "chat-1",
      work_plan: ["continue past result selection", "reach the payment step"],
    };

    await renderChat();

    await waitFor(() =>
      expect(screen.getByText("reach the payment step")).toBeTruthy(),
    );
    expect(screen.getByText("continue past result selection")).toBeTruthy();
  });

  it("renders the plan the turn ended with, without a reload or a history refetch", async () => {
    await renderChat();
    const readsBeforeTurn = historyGet.mock.calls.length;
    await submitTurn("book a seat and return the confirmation code");
    await finishTurn(0, [
      "continue past result selection",
      "reach the payment step",
    ]);

    await waitFor(() =>
      expect(screen.getByText("reach the payment step")).toBeTruthy(),
    );
    expect(screen.getByText("continue past result selection")).toBeTruthy();
    expect(historyGet.mock.calls.length).toBe(readsBeforeTurn);
  });

  it("replaces the rendered plan with the next turn's revision and clears it on an empty list", async () => {
    await renderChat();
    await submitTurn("start");
    await finishTurn(0, ["scout the search page"]);
    await waitFor(() =>
      expect(screen.getByText("scout the search page")).toBeTruthy(),
    );

    await submitTurn("keep going");
    await finishTurn(1, ["reach the payment step"]);
    await waitFor(() =>
      expect(screen.getByText("reach the payment step")).toBeTruthy(),
    );
    expect(screen.queryByText("scout the search page")).toBeNull();

    await submitTurn("done");
    await finishTurn(2, []);
    await waitFor(() =>
      expect(screen.queryByText("reach the payment step")).toBeNull(),
    );
  });

  it("drops the plan when the user starts a new chat", async () => {
    await renderChat();
    await submitTurn("start");
    await finishTurn(0, ["reach the payment step"]);
    await waitFor(() =>
      expect(screen.getByText("reach the payment step")).toBeTruthy(),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });

    await waitFor(() =>
      expect(screen.queryByText("reach the payment step")).toBeNull(),
    );
  });

  it("leaves the rendered plan standing when a terminal frame carries no plan snapshot", async () => {
    await renderChat();
    await submitTurn("start");
    await finishTurn(0, ["reach the payment step"]);
    await waitFor(() =>
      expect(screen.getByText("reach the payment step")).toBeTruthy(),
    );

    await submitTurn("what does this do?");
    await finishTurn(1, null);

    expect(screen.getByText("reach the payment step")).toBeTruthy();
  });
});
