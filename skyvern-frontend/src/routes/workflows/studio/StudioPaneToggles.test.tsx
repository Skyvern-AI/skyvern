// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioPaneToggles } from "./StudioPaneToggles";

const { runsQueryMock, runWithWorkflowMock, infiniteRunsMock } = vi.hoisted(
  () => ({
    runsQueryMock: vi.fn(),
    runWithWorkflowMock: vi.fn(),
    infiniteRunsMock: vi.fn(),
  }),
);

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));

vi.mock("../hooks/useInfiniteWorkflowRunsQuery", () => ({
  useInfiniteWorkflowRunsQuery: () => infiniteRunsMock(),
}));

function infiniteRuns(runs: Array<Record<string, unknown>>) {
  return {
    data: { pages: [runs] },
    isError: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
}

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => runWithWorkflowMock(),
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

function renderAt(path = "/workflows/wpid_abc/studio") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      {/* The shell provides this in production (StudioShell root). */}
      <TooltipProvider delayDuration={0}>
        <StudioPaneToggles />
      </TooltipProvider>
      <LocationProbe />
    </MemoryRouter>,
  );
}

function tab(name: RegExp | string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

function currentPanes(): string | null {
  const search = screen.getByTestId("search").textContent ?? "";
  return new URLSearchParams(search).get("panes");
}

afterEach(cleanup);
beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  runsQueryMock.mockReturnValue({ data: [] });
  runWithWorkflowMock.mockReturnValue({ data: undefined });
  infiniteRunsMock.mockReturnValue(infiniteRuns([]));
});

describe("StudioPaneToggles structure", () => {
  test("renders the four peer tabs with icon + label", () => {
    renderAt();
    for (const label of ["Copilot", "Editor", "Browser", "Past Runs"]) {
      expect(tab(new RegExp(`^${label}`))).toBeTruthy();
    }
  });

  test("reflects the default panes (editor + browser) as expanded", () => {
    renderAt();
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
  });

  test("reflects an explicit ?panes= list", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("false");
  });

  test("explicit ?panes= wins over a run deep link", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&panes=copilot");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
    // The Past Runs tab is a popover trigger; its active state (run pane open)
    // is aria-pressed, not aria-expanded.
    expect(tab(/^Past Runs/).getAttribute("aria-pressed")).toBe("false");
  });

  test("a block-run deep link opens Editor, Browser and the run pane", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Past Runs/).getAttribute("aria-pressed")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
  });
});

describe("StudioPaneToggles pane toggling", () => {
  test("opening a closed pane appends it in click order", () => {
    renderAt();
    fireEvent.click(tab(/^Copilot/));
    expect(currentPanes()).toBe("editor,browser,copilot");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
  });

  test("closing an open pane splices it out, keeping the rest in order", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor,copilot,browser");
    fireEvent.click(tab(/^Copilot/));
    expect(currentPanes()).toBe("editor,browser");
  });

  test("closing the last pane leaves an explicit empty list", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor");
    fireEvent.click(tab(/^Editor/));
    expect(currentPanes()).toBe("");
  });

  test("toggling preserves unrelated params", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    fireEvent.click(tab(/^Copilot/));
    const search = screen.getByTestId("search").textContent ?? "";
    const params = new URLSearchParams(search);
    expect(params.get("wr")).toBe("run_1");
    expect(params.get("bl")).toBe("block_1");
    expect(params.get("panes")).toBe("editor,browser,overview,copilot");
  });
});

describe("StudioPaneToggles run selector", () => {
  test("the Past Runs tab is enabled even with no runs", () => {
    renderAt();
    expect(tab(/^Past Runs/).disabled).toBe(false);
  });

  test("clicking the Past Runs tab opens the selector without toggling the pane", async () => {
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    fireEvent.click(tab(/^Past Runs/));
    // The selector popover opens (its "Past runs" header renders)...
    expect(await screen.findByText("Past runs")).toBeTruthy();
    // ...and the run pane is never toggled onto the stage by opening it.
    expect(currentPanes()).toBe("copilot");
  });

  test("selecting a run opens the run pane and closes the popover", async () => {
    infiniteRunsMock.mockReturnValue(
      infiniteRuns([
        {
          workflow_run_id: "wr_pick",
          status: Status.Completed,
          created_at: "2026-07-20T00:00:00Z",
        },
      ]),
    );
    // A different current run so the picked row is clickable (not the current).
    renderAt("/workflows/wpid_abc/studio?panes=copilot&wr=wr_other");
    fireEvent.click(tab(/^Past Runs/));
    fireEvent.click(await screen.findByText("wr_pick"));

    // Selecting opens the run pane (overview). The row's switchRun also sets
    // ?wr= (covered in PastRunsList.test); under MemoryRouter window.location
    // doesn't sync between the two navigations, so ?wr= can't be co-asserted
    // here, but the openPane merge is exercised.
    expect(currentPanes()?.split(",")).toContain("overview");
    await waitFor(() => expect(screen.queryByText("wr_pick")).toBeNull());
  });

  test("selecting the already-viewed run reopens the closed run pane", async () => {
    // ?wr=wr_same names the current run but overview is NOT open (pane closed via
    // its ✕). Clicking the current row must still reopen the pane.
    infiniteRunsMock.mockReturnValue(
      infiniteRuns([
        {
          workflow_run_id: "wr_same",
          status: Status.Completed,
          created_at: "2026-07-20T00:00:00Z",
        },
      ]),
    );
    renderAt("/workflows/wpid_abc/studio?panes=copilot&wr=wr_same");
    fireEvent.click(tab(/^Past Runs/));
    fireEvent.click(await screen.findByText("wr_same"));

    expect(currentPanes()?.split(",")).toContain("overview");
  });
});

describe("StudioPaneToggles run-status dot", () => {
  test("shows a status-colored dot for a finalized run", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt();
    const dot = tab(/^Past Runs/).querySelector(
      "span.absolute.-right-0\\.5",
    ) as HTMLElement | null;
    expect(dot).not.toBeNull();
    expect(dot?.className).toContain("bg-badge-success");
  });

  test("includes the finalized run status in the Past Runs tab accessible name", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.TimedOut }] });
    renderAt();
    expect(
      screen.getByRole("button", { name: "Past Runs, timed out" }),
    ).toBeTruthy();
  });

  test("omits the dot while the run is still in flight", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Running }] });
    renderAt();
    expect(
      tab(/^Past Runs/).querySelector("span.absolute.-right-0\\.5"),
    ).toBeNull();
  });
});

describe("StudioPaneToggles browser activity", () => {
  test("exposes unseen activity on the Browser tab while its pane is closed", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    expect(
      screen.getByRole("button", { name: "Browser, new activity" }),
    ).toBeTruthy();
  });

  test("hides the activity dot while the Browser pane is open", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=browser");
    expect(
      screen.queryByRole("button", { name: "Browser, new activity" }),
    ).toBeNull();
  });

  test("clears unseen activity when the Browser pane is opened", () => {
    useStudioBrowserStore.getState().markActivity();
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    fireEvent.click(tab(/Browser/));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    expect(currentPanes()).toBe("copilot,browser");
  });
});

describe("StudioPaneToggles keyboard navigation", () => {
  test("the rail is a single tab stop (roving tabindex)", () => {
    renderAt();
    expect(
      ["Copilot", "Editor", "Browser"].map(
        (l) => tab(new RegExp(`^${l}`)).tabIndex,
      ),
    ).toEqual([0, -1, -1]);
  });

  test("ArrowRight moves across all four tabs and wraps", () => {
    renderAt();
    tab(/^Copilot/).focus();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Editor/));
    fireEvent.keyDown(tab(/^Editor/), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Browser/));
    fireEvent.keyDown(tab(/^Browser/), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Past Runs/));
    fireEvent.keyDown(tab(/^Past Runs/), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("ArrowLeft wraps to the last tab and Home returns to the first", () => {
    renderAt();
    tab(/^Copilot/).focus();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowLeft" });
    expect(document.activeElement).toBe(tab(/^Past Runs/));
    fireEvent.keyDown(tab(/^Past Runs/), { key: "Home" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("arrow keys move focus without toggling panes", () => {
    renderAt();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowRight" });
    expect(currentPanes()).toBeNull();
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
  });
});
