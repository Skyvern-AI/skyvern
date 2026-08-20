// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Status, type TaskApiResponse } from "@/api/types";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mockGet }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => true,
}));

vi.mock("@/components/StatusFilterDropdown", () => ({
  StatusFilterDropdown: () => <div data-testid="status-filter" />,
}));

vi.mock("@/routes/workflows/components/tagging/TagChipList", () => ({
  TagChipList: ({
    tags,
  }: {
    tags: Array<{ key: string | null; value: string }>;
  }) => (
    <span data-testid="tag-chip-list">
      {tags.map((tag) => `${tag.key ?? "label"}:${tag.value}`).join(",")}
    </span>
  ),
}));

vi.mock("@/routes/workflows/hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({
    data: [{ key: "env", description: "Environment", workflow_count: 1 }],
  }),
}));

vi.mock("@/routes/workflows/hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("../hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({
    data: { wr_1: [{ key: "env", value: "prod" }] },
    isPending: false,
  }),
}));

vi.mock("./TaskActions", () => ({
  TaskActions: ({ taggingEnabled }: { taggingEnabled?: boolean }) => (
    <span data-testid="task-actions-tags">
      {taggingEnabled ? "tags-enabled" : "tags-disabled"}
    </span>
  ),
}));

import { TaskHistory } from "./TaskHistory";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("TaskHistory run tags", () => {
  it("renders run tag chips and tag-enabled row actions", async () => {
    mockGet.mockResolvedValue({
      data: [
        {
          task_id: "task_1",
          workflow_run_id: "wr_1",
          status: Status.Completed,
          created_at: "2026-07-08T00:00:00Z",
          request: { url: "https://example.test" },
          failure_reason: null,
        } as TaskApiResponse,
      ],
    });

    render(<TaskHistory />, { wrapper });

    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith("/tasks", expect.anything()),
    );
    expect(screen.getByTestId("tag-chip-list").textContent).toContain(
      "env:prod",
    );
    expect(screen.getByTestId("task-actions-tags").textContent).toBe(
      "tags-enabled",
    );
  });
});
