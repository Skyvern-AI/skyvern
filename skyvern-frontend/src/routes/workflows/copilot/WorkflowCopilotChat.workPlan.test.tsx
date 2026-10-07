import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
    const get = vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        url.startsWith("/artifacts/")
          ? {
              data: {
                artifact_id: url.split("/")[2],
                signed_url: `https://files.test${url}.png`,
              },
            }
          : history,
      ),
    );
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

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat() {
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <FeatureFlagContext.Provider value={() => false}>
        <WorkflowCopilotChat />
      </FeatureFlagContext.Provider>
    </QueryClientProvider>,
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

describe("WorkflowCopilotChat — a plan renders in the turn that wrote it", () => {
  const planTurn = (
    turnId: string,
    callId: string,
    items: string[],
    summary: string,
    createdAt: string,
  ) => ({
    sender: "ai",
    content: summary,
    created_at: createdAt,
    turn_outcome: { copilot_turn_id: turnId, terminal_reason: "completed" },
    narrative_payload: {
      turnId,
      turnIndex: 0,
      designStarted: true,
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["search"] },
      blocks: [],
      terminal: "response",
      terminalMessage: summary,
      narrativeSummary: summary,
      designActivity: [
        {
          kind: "tool_call",
          id: `tc-${callId}`,
          text: "Updating its plan…",
          toolName: "set_work_plan",
          iteration: 0,
        },
        {
          kind: "tool_result",
          id: `tr-${callId}`,
          text: "Updated its plan",
          toolName: "set_work_plan",
          iteration: 0,
          success: true,
        },
      ],
      workPlan: { toolCallId: callId, items },
    },
  });

  it("places each revision after its own plan row, folds the one it replaced, and drops the chat-level card", async () => {
    historyResponse.data = {
      ...historyResponse.data,
      workflow_copilot_chat_id: "chat-1",
      work_plan: ["scout the search page", "pay with the saved card"],
      chat_history: [
        {
          sender: "user",
          content: "start",
          created_at: "2026-09-04T00:00:00Z",
        },
        planTurn(
          "turn-1",
          "p1",
          ["scout the search page", "reach the payment step"],
          "Drafted the search.",
          "2026-09-04T00:00:01Z",
        ),
        {
          sender: "user",
          content: "use the saved card",
          created_at: "2026-09-04T00:00:02Z",
        },
        planTurn(
          "turn-2",
          "p2",
          ["scout the search page", "pay with the saved card"],
          "Switched to the saved card.",
          "2026-09-04T00:00:03Z",
        ),
      ],
    };

    await renderChat();

    const summary = await screen.findByText("Switched to the saved card.");
    const revision = screen.getByText("pay with the saved card");
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const planRow = document.querySelector('[data-activity-row-id="p2"]')!;
    expect(precedes(planRow, revision)).toBe(true);
    expect(precedes(revision, summary)).toBe(true);
    expect(
      screen.getAllByRole("group", { name: "Copilot's plan" }),
    ).toHaveLength(2);
    // The first plan folded when the user answered it; its step shows only as the revision's removal.
    expect(screen.getAllByText("reach the payment step")).toHaveLength(1);
    expect(screen.getByText("Plan updated")).toBeTruthy();
  });

  it("shows a live plan after its row and keeps it once the turn ends, even when the cap trimmed that row", async () => {
    await renderChat();
    await submitTurn("book a seat");
    const plan = ["scout the search page", "reach the payment step"];
    const call = (id: string, toolName: string) => ({
      tool_name: toolName,
      display_label: toolName,
      iteration: 0,
      tool_call_id: id,
    });
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-09-04T00:00:00Z",
      });
      streamCalls[0]!.onMessage({ type: "design_start" });
      for (const [id, toolName] of [
        ["p1", "set_work_plan"],
        ["w1", "update_workflow"],
      ]) {
        streamCalls[0]!.onMessage({
          type: "tool_call",
          tool_input: {},
          ...call(id!, toolName!),
        });
        streamCalls[0]!.onMessage({
          type: "tool_result",
          success: true,
          summary: "done",
          work_plan: toolName === "set_work_plan" ? plan : null,
          ...call(id!, toolName!),
        });
      }
    });
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const row = (id: string) =>
      document.querySelector(`[data-activity-row-id="${id}"]`)!;
    const livePlan = await screen.findByRole("group", {
      name: "Copilot's plan",
    });
    expect(precedes(row("p1"), livePlan)).toBe(true);
    expect(precedes(livePlan, row("w1"))).toBe(true);

    await act(async () => {
      streamCalls[0]!.onMessage({
        ...terminalResponse(plan),
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          designEnded: true,
          draft: { blockCount: 1, blockLabels: ["search"] },
          blocks: [],
          terminal: "response",
          terminalMessage: "Saved a draft.",
          narrativeSummary: "Saved a draft.",
          designActivity: [
            {
              kind: "tool_result",
              id: "tr-w1",
              text: "Saved the draft",
              toolName: "update_workflow",
              iteration: 0,
              success: true,
            },
          ],
          workPlan: { toolCallId: "p1", items: plan },
        },
      });
      streamCalls[0]!.resolve();
    });
    const summary = await screen.findByText("Saved a draft.");
    const [finalPlan, ...extra] = screen.getAllByRole("group", {
      name: "Copilot's plan",
    });
    expect(extra).toHaveLength(0);
    expect(precedes(finalPlan!, summary)).toBe(true);
    expect(screen.getByText("reach the payment step")).toBeTruthy();
  });
});

describe("WorkflowCopilotChat — screenshots render where they were captured", () => {
  it("places frames after their steps, hides them under the fold, retries a failed image once, and opens one full size", async () => {
    await renderChat();
    await submitTurn("check the pricing page");
    const steps = [
      { id: "n1", toolName: "navigate_browser", at: "2026-09-04T00:00:01Z" },
      { id: "w1", toolName: "update_workflow", at: "2026-09-04T00:00:04Z" },
    ];
    // a_2 belongs to the first step but was captured after the second one started.
    const frames = [
      { artifactId: "a_1", capturedAt: "2026-09-04T00:00:02Z" },
      {
        artifactId: "a_2",
        capturedAt: "2026-09-04T00:00:04.500Z",
        toolCallId: "n1",
      },
      { artifactId: "a_3", capturedAt: "2026-09-04T00:00:05Z" },
    ];
    const send = (payload: unknown) => streamCalls[0]!.onMessage(payload);
    const runStep = (step: (typeof steps)[number]) => {
      const call = {
        tool_name: step.toolName,
        display_label: step.toolName,
        iteration: 0,
        tool_call_id: step.id,
        timestamp: step.at,
      };
      send({ type: "tool_call", tool_input: {}, ...call });
      send({ type: "tool_result", success: true, summary: "done", ...call });
    };
    const capture = (frame: (typeof frames)[number]) =>
      send({
        type: "screenshot",
        artifact_id: frame.artifactId,
        captured_at: frame.capturedAt,
        tool_call_id: frame.toolCallId,
      });
    await act(async () => {
      send({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        timestamp: "2026-09-04T00:00:00Z",
      });
      send({ type: "design_start" });
      runStep(steps[0]!);
      capture(frames[0]!);
      capture(frames[1]!);
      runStep(steps[1]!);
      capture(frames[2]!);
    });

    const thumbnails = () =>
      screen.queryAllByRole("button", { name: "View screenshot" });
    const precedes = (a: Node, b: Node) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const row = (id: string) =>
      document.querySelector(`[data-activity-row-id="${id}"]`)!;
    const expectEachAfterItsStep = () => {
      const [first, second, third] = thumbnails();
      expect(precedes(row("n1"), first!)).toBe(true);
      expect(second!.parentElement).toBe(first!.parentElement);
      expect(precedes(second!, row("w1"))).toBe(true);
      expect(precedes(row("w1"), third!)).toBe(true);
    };
    await waitFor(() => expect(thumbnails()).toHaveLength(3));
    expectEachAfterItsStep();

    await act(async () => {
      send({
        ...terminalResponse(null),
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          designEnded: true,
          draft: { blockCount: 1, blockLabels: ["search"] },
          blocks: [],
          terminal: "response",
          terminalMessage: "Saved a draft.",
          narrativeSummary: "Saved a draft.",
          designActivity: steps.map((step) => ({
            kind: "tool_result",
            id: `tr-${step.id}`,
            text: "done",
            toolName: step.toolName,
            iteration: 0,
            success: true,
            timestamp: step.at,
          })),
          screenshots: frames,
        },
      });
      streamCalls[0]!.resolve();
    });
    const fold = await screen.findByRole("button", {
      name: /^Worked through 2 steps/,
    });
    expect(thumbnails()).toHaveLength(0);

    fireEvent.click(fold);
    expect(thumbnails()).toHaveLength(3);
    expectEachAfterItsStep();

    const image = (index: number) => thumbnails()[index]?.querySelector("img");
    await waitFor(() => expect(image(2)).toBeTruthy());
    fireEvent.error(image(2)!);
    await waitFor(() =>
      expect(image(2)!.getAttribute("src")).toBe(
        "https://files.test/artifacts/a_3/signed-url.png",
      ),
    );
    fireEvent.error(image(2)!);
    expect(thumbnails()).toHaveLength(2);
    expect(screen.getByText("Screenshot unavailable")).toBeTruthy();

    fireEvent.click(thumbnails()[0]!);
    const dialog = screen.getByRole("dialog", { name: "Screenshot" });
    expect(within(dialog).getByRole("img").getAttribute("src")).toBe(
      "https://files.test/artifacts/a_1.png",
    );
  });
});
