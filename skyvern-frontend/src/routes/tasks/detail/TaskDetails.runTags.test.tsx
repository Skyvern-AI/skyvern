// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { Status, type TaskApiResponse } from "@/api/types";

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/hooks/useApiCredential", () => ({
  useApiCredential: () => "api-key",
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => true,
}));

vi.mock("@/hooks/useFirstParam", () => ({
  useFirstParam: () => "task_1",
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () =>
    Promise.resolve({
      get: mockGet,
      post: mockPost,
    }),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("@/components/ApiWebhookActionsMenu", () => ({
  ApiWebhookActionsMenu: () => <div data-testid="api-menu" />,
}));

vi.mock("@/components/WebhookReplayDialog", () => ({
  WebhookReplayDialog: () => null,
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
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("@/routes/workflows/hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("./hooks/useTaskQuery", () => ({
  useTaskQuery: () => ({
    data: {
      task_id: "task_1",
      workflow_run_id: "wr_1",
      status: Status.Completed,
      request: { url: "https://example.test" },
      extracted_information: null,
      failure_reason: null,
      failure_category: null,
      webhook_failure_reason: null,
      max_steps_per_run: null,
    } as TaskApiResponse,
    isLoading: false,
    isError: false,
  }),
}));

vi.mock("../hooks/useRunTagsQuery", () => ({
  useRunTagsQuery: () => ({
    data: [
      { key: "env", value: "prod" },
      { key: null, value: "urgent" },
    ],
  }),
}));

vi.mock("./TaskRunVerificationCodeForm", () => ({
  TaskRunVerificationCodeForm: () => null,
}));

import { TaskDetails } from "./TaskDetails";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const originalScrollIntoView = Element.prototype.scrollIntoView;

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  mockGet.mockImplementation((path: string) =>
    Promise.resolve({
      data:
        path === "/workflows/runs/wr_1"
          ? { workflow_id: "wpid_1", workflow_run_id: "wr_1" }
          : { title: "Workflow", workflow_permanent_id: "wpid_1" },
    }),
  );
  mockPost.mockResolvedValue({ data: { workflow_run_id: "wr_1", tags: [] } });
});

afterEach(() => {
  vi.clearAllMocks();
});

afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});

describe("TaskDetails run tags", () => {
  it("renders run tags and the tag picker trigger", () => {
    render(<TaskDetails />, { wrapper });

    expect(screen.getByTestId("tag-chip-list").textContent).toContain(
      "env:prod",
    );
    expect(screen.getByRole("button", { name: /tags/i })).not.toBeNull();
  });

  it("removes a current run tag through tags_to_delete", async () => {
    render(<TaskDetails />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /tags/i }));
    fireEvent.click(await screen.findByText("env: prod"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/runs/wr_1/tags", {
        tags_to_delete: [{ key: "env" }],
      }),
    );
  });

  it("removes a current label-only tag through tags_to_delete", async () => {
    render(<TaskDetails />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /tags/i }));
    fireEvent.click(await screen.findByText("urgent"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/runs/wr_1/tags", {
        tags_to_delete: [{ value: "urgent" }],
      }),
    );
  });
});
