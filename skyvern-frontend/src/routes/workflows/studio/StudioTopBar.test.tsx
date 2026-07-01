// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioTopBar } from "./StudioTopBar";

vi.mock("../editor/nodes/components/EditableNodeTitle", () => ({
  EditableNodeTitle: ({ value }: { value: string }) => <div>{value}</div>,
}));

vi.mock("../editor/header/EditorOverflowMenu", () => ({
  EditorOverflowMenu: () => <button type="button">More</button>,
}));

vi.mock("../editor/MakeACopyButton", () => ({
  MakeACopyButton: () => <button type="button">Make a copy</button>,
}));

vi.mock("../editor/hooks/useSaveWorkflow", () => ({
  useSaveWorkflow: () => vi.fn(),
}));

vi.mock("../hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({ data: undefined }),
}));

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: [] }),
}));

vi.mock("./useStudioRunId", () => ({
  useStudioRunId: () => null,
}));

const initialBrowserState = useStudioBrowserStore.getState();

function renderStudioTopBar() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/workflows/wpid_test/studio"]}>
        <Routes>
          <Route
            path="/workflows/:workflowPermanentId/studio"
            element={<StudioTopBar />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  useStudioShellStore.getState().reset();
});

describe("StudioTopBar browser activity indicator", () => {
  it("exposes unseen browser activity on the Browser tab and clears it when selected", () => {
    useStudioShellStore.getState().setTab("editor");
    useStudioBrowserStore.getState().markActivity();

    renderStudioTopBar();

    const browserTab = screen.getByRole("tab", {
      name: "Browser, new activity",
    });
    expect(browserTab.getAttribute("aria-selected")).toBe("false");

    fireEvent.click(browserTab);

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(
      screen
        .getByRole("tab", { name: "Browser" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("clears unseen browser activity on keyboard navigation to the Browser tab", () => {
    useStudioShellStore.getState().setTab("editor");
    useStudioBrowserStore.getState().markActivity();

    renderStudioTopBar();

    fireEvent.keyDown(screen.getByRole("tablist", { name: "Studio view" }), {
      key: "ArrowRight",
    });

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(
      screen
        .getByRole("tab", { name: "Browser" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });
});
