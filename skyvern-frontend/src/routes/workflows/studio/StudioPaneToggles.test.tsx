// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  BrowserRouter,
  MemoryRouter,
  parsePath,
  useLocation,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { StudioPaneToggles } from "./StudioPaneToggles";

const { runsQueryMock, runWithWorkflowMock, infiniteRunsMock, copyTextMock } =
  vi.hoisted(() => ({
    runsQueryMock: vi.fn(),
    runWithWorkflowMock: vi.fn(),
    infiniteRunsMock: vi.fn(),
    copyTextMock: vi.fn(),
  }));

vi.mock("@/util/copyText", () => ({
  copyText: (text: string) => copyTextMock(text),
}));

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
  return (
    <>
      <output data-testid="search">{location.search}</output>
      <output data-testid="route-state">
        {JSON.stringify(location.state ?? null)}
      </output>
    </>
  );
}

function ToggleHarness() {
  return (
    <>
      {/* The shell provides these in production (StudioShell root / the
          workflow route resolver). */}
      <WorkflowPermanentIdContext.Provider value="wpid_abc">
        <TooltipProvider delayDuration={0}>
          <StudioPaneToggles />
        </TooltipProvider>
      </WorkflowPermanentIdContext.Provider>
      <LocationProbe />
    </>
  );
}

function renderAt(path = "/workflows/wpid_abc/studio", state?: unknown) {
  return render(
    <MemoryRouter
      initialEntries={[
        state === undefined ? path : { ...parsePath(path), state },
      ]}
    >
      <ToggleHarness />
    </MemoryRouter>,
  );
}

function renderInBrowser(path: string, state: unknown) {
  window.history.replaceState({ usr: state, key: "seed", idx: 0 }, "", path);
  return render(
    <BrowserRouter>
      <ToggleHarness />
    </BrowserRouter>,
  );
}

function tab(name: RegExp | string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

// The primary run control reads "View Run: <id>" once a run is inspected and
// "Past Runs" otherwise.
function runsTab(): HTMLButtonElement {
  return (
    (screen.queryByRole("button", {
      name: /^View Run/,
    }) as HTMLButtonElement | null) ?? tab(/^Past Runs/)
  );
}

function currentPanes(): string | null {
  const search = screen.getByTestId("search").textContent ?? "";
  return new URLSearchParams(search).get("panes");
}

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  useStudioShellStore.getState().reset();
  window.history.replaceState(null, "", "/");
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

  test("the empty Past Runs control exposes its popover state and target", () => {
    renderAt();
    const trigger = tab(/^Past Runs/);

    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("aria-controls")).toBeTruthy();
  });

  test("explicit ?panes= wins over a run deep link", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&panes=copilot");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
    // The inspected-run control reports whether its pane is open.
    expect(runsTab().getAttribute("aria-pressed")).toBe("false");
  });

  test("a block-run deep link opens Editor, Browser and the run pane", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
    expect(tab(/^Browser/).getAttribute("aria-expanded")).toBe("true");
    expect(runsTab().getAttribute("aria-pressed")).toBe("true");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
  });
});

describe("StudioPaneToggles pane toggling", () => {
  test("opening Copilot updates runtime state without creating a panes param", () => {
    renderAt();
    fireEvent.click(tab(/^Copilot/));
    expect(currentPanes()).toBeNull();
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
  });

  test("closing Copilot keeps the committed panes URL unchanged", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor,copilot,browser");
    fireEvent.click(tab(/^Copilot/));
    expect(currentPanes()).toBe("editor,copilot,browser");
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("false");
  });

  test("closing the last pane leaves an explicit empty list", () => {
    renderAt("/workflows/wpid_abc/studio?panes=editor");
    fireEvent.click(tab(/^Editor/));
    expect(currentPanes()).toBe("");
  });

  test("Copilot toggling preserves unrelated params without adding panes", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=block_1");
    fireEvent.click(tab(/^Copilot/));
    const search = screen.getByTestId("search").textContent ?? "";
    const params = new URLSearchParams(search);
    expect(params.get("wr")).toBe("run_1");
    expect(params.get("bl")).toBe("block_1");
    expect(params.get("panes")).toBeNull();
    expect(tab(/^Copilot/).getAttribute("aria-expanded")).toBe("true");
  });
});

test("keeps MemoryRouter state when browser history has unrelated state", () => {
  window.history.replaceState(
    { usr: { source: "browser" }, key: "browser", idx: 0 },
    "",
    "/",
  );
  renderAt("/workflows/wpid_abc/studio?panes=editor", { source: "memory" });

  fireEvent.click(tab(/^Browser/));

  expect(currentPanes()).toBe("editor,browser");
  expect(screen.getByTestId("route-state").textContent).toBe(
    '{"source":"memory"}',
  );
});

describe("StudioPaneToggles run tab label", () => {
  test("names the URL's run with its full id in the top bar", () => {
    renderAt("/workflows/wpid_abc/studio?wr=wr_556219201027773764");
    expect(tab("View Run: wr_556219201027773764")).toBeTruthy();
  });

  test("the tab's copy affordance copies the run link without opening the selector", async () => {
    copyTextMock.mockResolvedValue(true);
    renderAt("/workflows/wpid_abc/studio?wr=wr_556219201027773764");

    fireEvent.click(screen.getByRole("button", { name: "Copy run link" }));

    await waitFor(() =>
      expect(copyTextMock).toHaveBeenCalledWith(
        `${window.location.origin}/agents/wpid_abc/studio?wr=wr_556219201027773764`,
      ),
    );
    // The click must not bubble into the popover trigger.
    expect(screen.queryByText("Past runs")).toBeNull();
  });

  test("no copy affordance renders while no run is inspected", () => {
    renderAt();
    expect(screen.queryByRole("button", { name: "Copy run link" })).toBeNull();
  });

  test("names the latest run when the URL names none", () => {
    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "wr_late", status: Status.Completed }],
    });
    renderAt();
    expect(tab(/^View Run: wr_late/)).toBeTruthy();
  });

  test("reads 'Past Runs' while no run exists to inspect", () => {
    renderAt();
    expect(tab(/^Past Runs/)).toBeTruthy();
  });
});

describe("StudioPaneToggles run selector", () => {
  test("the Past Runs tab is enabled even with no runs", () => {
    renderAt();
    expect(runsTab().disabled).toBe(false);
  });

  test("clicking the Past Runs tab opens the selector without toggling the pane", async () => {
    renderAt("/workflows/wpid_abc/studio?panes=copilot");
    fireEvent.click(runsTab());
    // The selector popover opens (its "Past runs" header renders)...
    expect(await screen.findByText("Past runs")).toBeTruthy();
    // ...and the run pane is never toggled onto the stage by opening it.
    expect(currentPanes()).toBe("copilot");
  });

  test("clicking the current run tab reopens its closed pane directly", () => {
    renderAt("/workflows/wpid_abc/studio?panes=copilot&wr=wr_current");

    fireEvent.click(tab("View Run: wr_current"));

    expect(currentPanes()?.split(",")).toContain("overview");
    expect(screen.queryByText("Past runs")).toBeNull();
  });

  test("clicking the current run tab closes its open pane", () => {
    renderAt("/workflows/wpid_abc/studio?panes=copilot,overview&wr=wr_current");

    fireEvent.click(tab("View Run: wr_current"));

    expect(currentPanes()?.split(",")).not.toContain("overview");
  });

  test("the separate Past runs button opens the selector without toggling the run pane", async () => {
    renderAt("/workflows/wpid_abc/studio?panes=copilot&wr=wr_current");

    fireEvent.click(tab("Past Runs"));

    expect(await screen.findByText("Past runs")).toBeTruthy();
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
    fireEvent.click(tab("Past Runs"));
    fireEvent.click(await screen.findByText("wr_pick"));

    // Selecting opens the run pane (overview). The row's switchRun also sets
    // ?wr= (covered in PastRunsList.test); under MemoryRouter window.location
    // doesn't sync between the two navigations, so ?wr= can't be co-asserted
    // here, but the openPane merge is exercised.
    expect(currentPanes()?.split(",")).toContain("overview");
    await waitFor(() => expect(screen.queryByText("wr_pick")).toBeNull());
  });

  test("does not carry the previous run's route state through a switch-then-open", async () => {
    infiniteRunsMock.mockReturnValue(
      infiniteRuns([
        {
          workflow_run_id: "wr_pick",
          status: Status.Completed,
          created_at: "2026-07-20T00:00:00Z",
        },
      ]),
    );
    renderInBrowser("/workflows/wpid_abc/studio?panes=copilot&wr=wr_other", {
      copilotMessage: "Fix run A",
    });

    fireEvent.click(tab("Past Runs"));
    fireEvent.click(await screen.findByText("wr_pick"));

    await waitFor(() => {
      const params = new URLSearchParams(
        screen.getByTestId("search").textContent ?? "",
      );
      expect(params.get("wr")).toBe("wr_pick");
      expect(params.get("panes")?.split(",")).toContain("overview");
      expect(screen.getByTestId("route-state").textContent).toBe("null");
    });
    expect(window.history.state.usr).toBeNull();
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
    fireEvent.click(tab("Past Runs"));
    fireEvent.click(await screen.findByText("wr_same"));

    expect(currentPanes()?.split(",")).toContain("overview");
  });
});

describe("StudioPaneToggles run-status dot", () => {
  test("shows a status-colored dot with a status icon for a finalized run", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Completed }] });
    renderAt();
    const dot = runsTab().querySelector(
      "span.absolute.-right-1",
    ) as HTMLElement | null;
    expect(dot).not.toBeNull();
    expect(dot?.className).toContain("bg-badge-success");
    expect(dot?.querySelector("svg")).not.toBeNull();
  });

  test("uses a different icon per finalized status (not color-only)", () => {
    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "wr_tab", status: Status.Failed }],
    });
    const { unmount } = renderAt();
    const failedIcon = runsTab().querySelector(
      "span.absolute.-right-1 svg",
    )?.outerHTML;
    unmount();
    cleanup();

    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "wr_tab", status: Status.Canceled }],
    });
    renderAt();
    const canceledIcon = runsTab().querySelector(
      "span.absolute.-right-1 svg",
    )?.outerHTML;

    expect(failedIcon).toBeTruthy();
    expect(canceledIcon).toBeTruthy();
    expect(failedIcon).not.toBe(canceledIcon);
  });

  test("includes the finalized run status in the run tab accessible name", () => {
    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "wr_tab", status: Status.TimedOut }],
    });
    renderAt();
    expect(
      screen.getByRole("button", { name: "View Run: wr_tab, timed out" }),
    ).toBeTruthy();
  });

  test("omits the dot while the run is still in flight", () => {
    runsQueryMock.mockReturnValue({ data: [{ status: Status.Running }] });
    renderAt();
    expect(runsTab().querySelector("span.absolute.-right-1")).toBeNull();
  });

  test("tooltips the run status even with labels expanded (no hidden xl:inline gate)", async () => {
    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "wr_tip", status: Status.Failed }],
    });
    renderAt();
    fireEvent.focus(tab(/^View Run: wr_tip/));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain("failed");
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
    expect(document.activeElement).toBe(runsTab());
    fireEvent.keyDown(runsTab(), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("ArrowRight includes the separate Past runs control while inspecting a run", () => {
    renderAt("/workflows/wpid_abc/studio?wr=wr_current");
    fireEvent.focus(tab(/^Browser/));

    fireEvent.keyDown(tab(/^Browser/), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab("View Run: wr_current"));
    fireEvent.keyDown(tab("View Run: wr_current"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab("Past Runs"));
    fireEvent.keyDown(tab("Past Runs"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("ArrowLeft wraps to the last tab and Home returns to the first", () => {
    renderAt();
    tab(/^Copilot/).focus();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowLeft" });
    expect(document.activeElement).toBe(runsTab());
    fireEvent.keyDown(runsTab(), { key: "Home" });
    expect(document.activeElement).toBe(tab(/^Copilot/));
  });

  test("arrow keys move focus without toggling panes", () => {
    renderAt();
    fireEvent.keyDown(tab(/^Copilot/), { key: "ArrowRight" });
    expect(currentPanes()).toBeNull();
    expect(tab(/^Editor/).getAttribute("aria-expanded")).toBe("true");
  });
});
