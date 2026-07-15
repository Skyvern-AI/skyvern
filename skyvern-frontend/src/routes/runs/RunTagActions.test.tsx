// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
  mutate: vi.fn(),
}));

vi.mock("@/components/ui/dropdown-menu", () => {
  const Pass = ({ children }: { children: ReactNode }) => <>{children}</>;
  return {
    DropdownMenu: Pass,
    DropdownMenuContent: Pass,
    DropdownMenuPortal: Pass,
    DropdownMenuSub: Pass,
    DropdownMenuSubContent: Pass,
    DropdownMenuSubTrigger: Pass,
    DropdownMenuTrigger: Pass,
  };
});

vi.mock("@/components/ui/context-menu", () => {
  const Pass = ({ children }: { children: ReactNode }) => <>{children}</>;
  return {
    ContextMenu: Pass,
    ContextMenuContent: Pass,
    ContextMenuItem: ({
      children,
      onSelect,
    }: {
      children: ReactNode;
      onSelect?: () => void;
    }) => <button onClick={onSelect}>{children}</button>,
    ContextMenuLabel: Pass,
    ContextMenuSeparator: () => null,
    ContextMenuSub: Pass,
    ContextMenuSubContent: Pass,
    ContextMenuSubTrigger: Pass,
    ContextMenuTrigger: Pass,
  };
});

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ post: mocks.post }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagMutations", () => ({
  invalidateRunTagQueries: vi.fn(),
  useApplyRunTagsMutation: () => ({
    mutate: mocks.mutate,
    isPending: false,
  }),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

import { RunBulkActionBar } from "./RunBulkActionBar";
import { RunRowContextMenu } from "./RunRowContextMenu";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", MockResizeObserver);
Element.prototype.scrollIntoView = () => {};

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

const runTagsMap = {
  wr_1: [
    { key: "env", value: "prod" },
    { key: "skyvern.platform", value: "browser" },
  ],
  wr_2: [
    { key: "env", value: "staging" },
    { key: null, value: "urgent" },
  ],
};

afterEach(() => {
  vi.clearAllMocks();
  mocks.post.mockResolvedValue({ data: {} });
});

describe("RunBulkActionBar", () => {
  it("adds to every selected run and removes only exact-tag matches", async () => {
    render(
      <RunBulkActionBar
        selectedRunIds={["wr_1", "wr_2"]}
        runTagsMap={runTagsMap}
        tagKeys={[]}
        labelSuggestions={["urgent", "reviewed"]}
        onClearSelection={vi.fn()}
      />,
      { wrapper },
    );

    fireEvent.click(screen.getAllByRole("option", { name: "urgent" })[0]!);
    await waitFor(() =>
      expect(mocks.post).toHaveBeenCalledWith("/runs/wr_2/tags", {
        tags_to_delete: [{ value: "urgent" }],
      }),
    );
    expect(mocks.post).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByPlaceholderText(/label or group/i), {
      target: { value: "reviewed" },
    });
    fireEvent.click(screen.getByRole("option", { name: /Add reviewed/i }));
    await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(3));
    expect(mocks.post).toHaveBeenCalledWith("/runs/wr_1/tags", {
      tags: [{ key: null, value: "reviewed" }],
    });
    expect(mocks.post).toHaveBeenCalledWith("/runs/wr_2/tags", {
      tags: [{ key: null, value: "reviewed" }],
    });
  });

  it("does not expose reserved tags for bulk removal", () => {
    render(
      <RunBulkActionBar
        selectedRunIds={["wr_1", "wr_2"]}
        runTagsMap={runTagsMap}
        tagKeys={[]}
        labelSuggestions={[]}
        onClearSelection={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.queryByText(/skyvern\.platform/)).toBeNull();
  });
});

describe("RunRowContextMenu", () => {
  it("manages the clicked run and warns when a bulk selection is active", () => {
    const onNavigate = vi.fn();
    render(
      <RunRowContextMenu
        workflowRunId="wr_1"
        runPath="/runs/wr_1"
        currentTags={runTagsMap.wr_1}
        tagKeys={[]}
        labelSuggestions={[]}
        selectedCount={2}
        onNavigate={onNavigate}
      >
        <div>Run row</div>
      </RunRowContextMenu>,
    );

    expect(screen.getByText(/Acts on this run only/)).toBeTruthy();
    fireEvent.click(screen.getByText("env: prod"));

    expect(mocks.mutate).toHaveBeenCalledWith(
      {
        workflowRunId: "wr_1",
        data: { tags_to_delete: [{ key: "env" }] },
      },
      expect.any(Object),
    );
    expect(screen.queryByText(/skyvern\.platform/)).toBeNull();
  });
});
