// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioStageLauncher } from "./StudioStageLauncher";

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: [] }),
}));

vi.mock("../hooks/useInfiniteWorkflowRunsQuery", () => ({
  useInfiniteWorkflowRunsQuery: () => ({
    data: { pages: [[]] },
    isError: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
}));

// Radix Popover positioning observes the anchor; jsdom has no ResizeObserver.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

const initialBrowserState = useStudioBrowserStore.getState();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="search">{location.search}</output>;
}

function renderAt(path = "/workflows/wpid_1/studio?panes=") {
  return render(
    <TooltipProvider delayDuration={0}>
      <MemoryRouter initialEntries={[path]}>
        <StudioStageLauncher />
        <LocationProbe />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

function currentPanes(): string | null {
  const search = screen.getByTestId("search").textContent ?? "";
  return new URLSearchParams(search).get("panes");
}

afterEach(cleanup);
beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
});

describe("StudioStageLauncher", () => {
  test("offers every pane as a labeled button", () => {
    renderAt();
    for (const label of ["Copilot", "Editor", "Browser", "Past Runs"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`^${label}`) }),
      ).toBeTruthy();
    }
  });

  test("the Past Runs tile is enabled even with no runs", () => {
    renderAt();
    const tile = screen.getByRole("button", {
      name: /^Past Runs/,
    }) as HTMLButtonElement;
    expect(tile.disabled).toBe(false);
  });

  test("clicking the Past Runs tile opens the selector, not the pane", async () => {
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: /^Past Runs/ }));
    // The selector popover opens; the run pane isn't opened directly.
    expect(await screen.findByText("Past runs")).toBeTruthy();
    expect(currentPanes()).toBe("");
  });

  test("opens the clicked pane", () => {
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: "Editor" }));
    expect(currentPanes()).toBe("editor");
  });

  test("clears unseen browser activity when opening the Browser pane", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt();
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(currentPanes()).toBe("browser");
  });
});
