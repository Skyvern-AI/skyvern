// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowCopilotHistory } from "./WorkflowCopilotHistory";
import { WorkflowCopilotChatSummary } from "./workflowCopilotTypes";

const { mockHook } = vi.hoisted(() => ({ mockHook: vi.fn() }));

vi.mock("./useInfiniteCopilotChatsQuery", () => ({
  useInfiniteCopilotChatsQuery: mockHook,
}));

const chats: WorkflowCopilotChatSummary[] = [
  {
    workflow_copilot_chat_id: "wcc_1",
    workflow_permanent_id: "wpid_a",
    workflow_title: "Login flow",
    title: "Build a login flow",
    created_at: "2026-06-20T10:00:00Z",
    modified_at: "2026-06-20T10:05:00Z",
  },
  {
    workflow_copilot_chat_id: "wcc_2",
    workflow_permanent_id: "wpid_b",
    workflow_title: "Scraper",
    title: "Scrape the news",
    created_at: "2026-06-21T10:00:00Z",
    modified_at: "2026-06-21T10:05:00Z",
  },
];

function mockChats(pages: WorkflowCopilotChatSummary[][]) {
  mockHook.mockReturnValue({
    data: { pages },
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    isFetching: false,
  });
}

afterEach(cleanup);
beforeEach(() => mockHook.mockReset());

describe("WorkflowCopilotHistory", () => {
  it("renders the workflow's chats when opened", async () => {
    mockChats([chats]);
    render(
      <WorkflowCopilotHistory
        workflowPermanentId="wpid_a"
        currentChatId={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /history/i }));

    expect(await screen.findByText("Build a login flow")).toBeTruthy();
    expect(screen.getByText("Scrape the news")).toBeTruthy();
  });

  it("calls onSelect with the chosen chat", async () => {
    mockChats([chats]);
    const onSelect = vi.fn();
    render(
      <WorkflowCopilotHistory
        workflowPermanentId="wpid_a"
        currentChatId={null}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    fireEvent.click(await screen.findByText("Scrape the news"));

    expect(onSelect).toHaveBeenCalledWith(chats[1]);
  });

  it("shows an empty state when there are no chats", async () => {
    mockChats([[]]);
    render(
      <WorkflowCopilotHistory
        workflowPermanentId="wpid_a"
        currentChatId={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /history/i }));

    expect(await screen.findByText("No chats found")).toBeTruthy();
  });

  it("feeds the workflow id and debounced search into the query", async () => {
    mockChats([chats]);
    render(
      <WorkflowCopilotHistory
        workflowPermanentId="wpid_a"
        currentChatId={null}
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /history/i }));
    const input = await screen.findByPlaceholderText("Search chats...");
    fireEvent.change(input, { target: { value: "deploy" } });

    await waitFor(
      () =>
        expect(mockHook).toHaveBeenCalledWith(
          expect.objectContaining({
            workflow_permanent_id: "wpid_a",
            search: "deploy",
          }),
        ),
      { timeout: 1500 },
    );
  });
});
