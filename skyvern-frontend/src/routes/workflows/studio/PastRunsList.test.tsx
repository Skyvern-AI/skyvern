// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useRunViewStore } from "@/store/RunViewStore";

import { PastRunsList } from "./PastRunsList";
import { searchWithRunSwitched } from "./runSwitchNavigation";

const mocks = vi.hoisted(() => ({
  runs: undefined as Array<Record<string, unknown>> | undefined,
  isError: false,
  hasNextPage: false,
  isFetchingNextPage: false,
  fetchNextPage: vi.fn(),
  lastWorkflowPermanentId: undefined as string | undefined,
}));

vi.mock("../hooks/useInfiniteWorkflowRunsQuery", () => ({
  useInfiniteWorkflowRunsQuery: (props: { workflowPermanentId?: string }) => {
    mocks.lastWorkflowPermanentId = props?.workflowPermanentId;
    return {
      data: mocks.runs === undefined ? undefined : { pages: [mocks.runs] },
      isError: mocks.isError,
      hasNextPage: mocks.hasNextPage,
      isFetchingNextPage: mocks.isFetchingNextPage,
      fetchNextPage: mocks.fetchNextPage,
    };
  },
}));

// useStudioInspectedRun's latest-run fallback reads the single-page query.
vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: mocks.runs ?? [] }),
}));

function LocationSpy() {
  const location = useLocation();
  return <div data-testid="search">{location.search}</div>;
}

const onSelect = vi.fn();

function renderList(entry = "/agents/wpid_1") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/agents/:workflowPermanentId"
          element={
            <>
              <PastRunsList open onSelect={onSelect} />
              <LocationSpy />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    created_at: "2026-07-20T00:00:00Z",
    ...overrides,
  };
}

function searchParams(): URLSearchParams {
  return new URLSearchParams(screen.getByTestId("search").textContent ?? "");
}

afterEach(() => {
  cleanup();
  mocks.runs = undefined;
  mocks.isError = false;
  mocks.hasNextPage = false;
  mocks.isFetchingNextPage = false;
  mocks.fetchNextPage.mockReset();
  mocks.lastWorkflowPermanentId = undefined;
  onSelect.mockReset();
});
beforeEach(() => {
  useRunViewStore.getState().reset();
});

describe("PastRunsList", () => {
  test("does not flash the empty state while runs are loading", () => {
    mocks.runs = undefined;
    renderList();
    expect(screen.queryByText("No runs yet")).toBeNull();
  });

  test("shows the empty state when the workflow has no runs", () => {
    mocks.runs = [];
    renderList();
    expect(screen.getByText("No runs yet")).toBeTruthy();
  });

  test("shows an error state (not a permanent spinner) when the query fails", () => {
    mocks.runs = undefined;
    mocks.isError = true;
    renderList();
    expect(screen.getByText("Couldn't load runs")).toBeTruthy();
    expect(screen.queryByText("No runs yet")).toBeNull();
  });

  test("renders a row per run with its id and status", () => {
    mocks.runs = [
      makeRun({ workflow_run_id: "wr_1", status: Status.Completed }),
      makeRun({ workflow_run_id: "wr_2", status: Status.Failed }),
    ];
    renderList();
    expect(screen.getByText("wr_1")).toBeTruthy();
    expect(screen.getByText("wr_2")).toBeTruthy();
    expect(screen.getByText("completed")).toBeTruthy();
    expect(screen.getByText("failed")).toBeTruthy();
  });

  test("shows the Past runs header, run count, and a View all runs link", () => {
    mocks.runs = [
      makeRun({ workflow_run_id: "wr_1" }),
      makeRun({ workflow_run_id: "wr_2" }),
    ];
    renderList();
    expect(screen.getByText("Past runs")).toBeTruthy();
    expect(screen.getByText("2 runs")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "View all runs" }).getAttribute("href"),
    ).toBe("/agents/wpid_1/runs");
  });

  test("shows a floor count ('20+ runs') while more pages exist", () => {
    mocks.runs = Array.from({ length: 20 }, (_, i) =>
      makeRun({ workflow_run_id: `wr_${i}` }),
    );
    mocks.hasNextPage = true;
    renderList();
    expect(screen.getByText("20+ runs")).toBeTruthy();
  });

  test("highlights the currently-inspected run", () => {
    mocks.runs = [
      makeRun({ workflow_run_id: "wr_1" }),
      makeRun({ workflow_run_id: "wr_2" }),
    ];
    renderList("/agents/wpid_1?wr=wr_2");
    expect(
      screen.getByText("wr_2").closest("button")?.getAttribute("aria-current"),
    ).toBe("true");
    expect(
      screen.getByText("wr_1").closest("button")?.getAttribute("aria-current"),
    ).toBeNull();
  });

  test("highlights the latest-run fallback when the URL names no run", () => {
    // No ?wr=: the Run pane inspects the latest run (useStudioInspectedRun), so
    // its row here is the current one too.
    mocks.runs = [
      makeRun({ workflow_run_id: "wr_1" }),
      makeRun({ workflow_run_id: "wr_2" }),
    ];
    renderList("/agents/wpid_1");
    expect(
      screen.getByText("wr_1").closest("button")?.getAttribute("aria-current"),
    ).toBe("true");
  });

  test("clicking a different run switches the run and signals close", () => {
    mocks.runs = [makeRun({ workflow_run_id: "wr_2" })];
    useRunViewStore.getState().pinFrame("act_5");
    renderList("/agents/wpid_1?panes=browser&wr=wr_9&active=act_5&bl=blk");

    fireEvent.click(screen.getByText("wr_2"));

    const params = searchParams();
    expect(params.get("wr")).toBe("wr_2");
    expect(params.get("panes")).toBe("browser");
    expect(params.get("active")).toBeNull();
    expect(params.get("bl")).toBeNull();
    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  test("clicking the already-viewed run reopens the pane without switching", () => {
    // The pane may be closed while ?wr= still names this run — clicking it must
    // reopen the pane (onSelect) even though the run doesn't change.
    mocks.runs = [makeRun({ workflow_run_id: "wr_1" })];
    renderList("/agents/wpid_1?wr=wr_1");

    fireEvent.click(screen.getByText("wr_1"));

    // No run change (?wr= unchanged), but onSelect fires to reopen the pane.
    expect(searchParams().get("wr")).toBe("wr_1");
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  test("resolves the workflow id from context on the short /runs/:runId route", () => {
    // The short run route has no :workflowPermanentId segment — the id arrives
    // through WorkflowPermanentIdContext. Reading it via useWorkflowPermanentId
    // (not raw useParams) keeps the runs query enabled there.
    mocks.runs = [makeRun({ workflow_run_id: "wr_1" })];
    render(
      <MemoryRouter initialEntries={["/runs/wr_1"]}>
        <Routes>
          <Route
            path="/runs/:runId"
            element={
              <WorkflowPermanentIdContext.Provider value="wpid_ctx">
                <PastRunsList open onSelect={onSelect} />
              </WorkflowPermanentIdContext.Provider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("wr_1")).toBeTruthy();
    expect(mocks.lastWorkflowPermanentId).toBe("wpid_ctx");
  });

  test("'View all runs' navigates without invoking onSelect", () => {
    mocks.runs = [makeRun({ workflow_run_id: "wr_1" })];
    renderList();
    fireEvent.click(screen.getByRole("link", { name: "View all runs" }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  test("shows a spinner row while fetching the next page", () => {
    mocks.runs = [makeRun({ workflow_run_id: "wr_1" })];
    mocks.hasNextPage = true;
    mocks.isFetchingNextPage = true;
    const { container } = renderList();
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  test("fetches the next page when scrolled near the bottom", () => {
    mocks.runs = Array.from({ length: 20 }, (_, i) =>
      makeRun({ workflow_run_id: `wr_${i}` }),
    );
    mocks.hasNextPage = true;
    const { container } = renderList();
    const scroller = container.querySelector(".overflow-y-auto") as HTMLElement;
    // jsdom reports 0 for scroll metrics; set them so the 0.8 threshold trips.
    Object.defineProperty(scroller, "scrollTop", { value: 100 });
    Object.defineProperty(scroller, "clientHeight", { value: 100 });
    Object.defineProperty(scroller, "scrollHeight", { value: 200 });
    fireEvent.scroll(scroller);
    expect(mocks.fetchNextPage).toHaveBeenCalled();
  });

  test("keeps the loaded rows when a background refetch errors", () => {
    // An infinite query retains its loaded pages when a background refetch
    // errors, so an error while data exists must not blank the list.
    mocks.runs = [makeRun({ workflow_run_id: "wr_1" })];
    mocks.isError = true;
    renderList();
    expect(screen.getByText("wr_1")).toBeTruthy();
    expect(screen.queryByText("Couldn't load runs")).toBeNull();
  });
});

describe("searchWithRunSwitched", () => {
  test("sets ?wr=, clears ?active=, ?bl= and ?iteration=, preserves ?panes=", () => {
    expect(
      searchWithRunSwitched(
        "?panes=browser&wr=wr_9&active=act_5&bl=blk&iteration=2",
        "wr_2",
      ),
    ).toBe("?panes=browser&wr=wr_2");
  });

  test("adds ?wr= when the url carries none, keeping other params", () => {
    const params = new URLSearchParams(
      searchWithRunSwitched("?panes=browser", "wr_7"),
    );
    expect(params.get("wr")).toBe("wr_7");
    expect(params.get("panes")).toBe("browser");
  });
});
