import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const jumpToLatest = vi.fn();
const { hookState } = vi.hoisted(() => ({ hookState: { isPinned: true } }));

vi.mock("./useStickToBottom", () => ({
  useStickToBottom: () => ({
    scrollRef: { current: null },
    isPinned: hookState.isPinned,
    jumpToLatest,
    repin: vi.fn(),
  }),
  computeFollowSignature: () => "sig",
}));

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming: vi.fn() }),
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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => ({}) }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat() {
  render(<WorkflowCopilotChat />);
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/Type your message/)).toBeTruthy(),
  );
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  jumpToLatest.mockClear();
  hookState.isPinned = true;
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — Jump to latest pill", () => {
  it("hides the pill while pinned", async () => {
    hookState.isPinned = true;
    await renderChat();
    expect(screen.queryByText(/Jump to latest/)).toBeNull();
  });

  it("shows the pill while disengaged and wires the click to jumpToLatest", async () => {
    hookState.isPinned = false;
    await renderChat();
    const pill = screen.getByText(/Jump to latest/);
    expect(pill).toBeTruthy();
    fireEvent.click(pill);
    expect(jumpToLatest).toHaveBeenCalledTimes(1);
  });
});
