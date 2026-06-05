import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

type StreamCall = {
  body: {
    message: string;
    workflow_id: string;
    workflow_permanent_id: string;
  };
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { streamCalls, postStreaming, mutateAsync, toast } = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const streaming = vi.fn(
    (
      _path: string,
      body: StreamCall["body"],
      onMessage: (payload: unknown) => boolean,
    ) =>
      new Promise<void>((resolve, reject) => {
        calls.push({ body, onMessage, resolve, reject });
      }),
  );
  return {
    streamCalls: calls,
    postStreaming: streaming,
    mutateAsync: vi.fn(),
    toast: vi.fn(),
  };
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
    post: vi.fn().mockResolvedValue({}),
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => ({
      workflowPermanentId: "new",
      workflowRunId: undefined,
    }),
  };
});

const draftSaveData = {
  title: "Handoff title",
  workflow: {
    workflow_id: "new",
    workflow_permanent_id: "new",
    description: "",
    totp_verification_url: null,
    is_saved_task: false,
    status: "draft",
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

const persistedWorkflow = {
  ...draftSaveData.workflow,
  workflow_id: "wf_real",
  workflow_permanent_id: "wpid_real",
  status: "published",
};

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({
    getSaveData: () => draftSaveData,
  }),
  useWorkflowSave: () => ({
    isPending: false,
    mutateAsync,
  }),
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

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  toast.mockClear();
  mutateAsync.mockResolvedValue({
    saveData: draftSaveData,
    createdWorkflow: persistedWorkflow,
    isDraft: true,
  });
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — draft auto-send", () => {
  it("persists a draft agent before auto-sending the Discover handoff prompt", async () => {
    render(<WorkflowCopilotChat initialMessage="Build this workflow" />);

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Save your agent first" }),
    );

    const streamBody = streamCalls[0]?.body;
    expect(streamBody?.message).toBe("Build this workflow");
    expect(streamBody?.workflow_id).toBe("wf_real");
    expect(streamBody?.workflow_permanent_id).toBe("wpid_real");

    await act(async () => {
      streamCalls[0]?.onMessage(turnStart());
      streamCalls[0]?.onMessage(terminalResponse("Done"));
      streamCalls[0]?.resolve();
    });

    expect(await screen.findByText("Done")).toBeTruthy();
  });
});
