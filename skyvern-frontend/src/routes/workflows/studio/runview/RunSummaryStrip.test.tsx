// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test } from "vitest";

import {
  Status,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";

import { RunSummaryStrip } from "./RunSummaryStrip";

afterEach(cleanup);

function makeRun(
  overrides: Partial<WorkflowRunStatusApiResponseWithWorkflow> = {},
): WorkflowRunStatusApiResponseWithWorkflow {
  return {
    workflow_run_id: "wr_123",
    status: Status.Completed,
    created_at: "2026-06-30T23:59:00Z",
    queued_at: "2026-06-30T23:59:30Z",
    started_at: "2026-07-01T00:00:00Z",
    finished_at: "2026-07-01T00:05:00Z",
    failure_category: null,
    browser_session_id: null,
    browser_profile_id: null,
    ...overrides,
  } as WorkflowRunStatusApiResponseWithWorkflow;
}

function renderStrip(run: WorkflowRunStatusApiResponseWithWorkflow) {
  return render(
    <MemoryRouter>
      {/* The studio shell provides this in production (StudioShell root). */}
      <TooltipProvider delayDuration={0}>
        <RunSummaryStrip workflowRun={run} />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

describe("RunSummaryStrip browser session/profile links", () => {
  test("links the run's browser session and profile at the legacy targets", () => {
    renderStrip(
      makeRun({
        browser_session_id: "pbs_abc",
        browser_profile_id: "bp_def",
      }),
    );
    expect(
      screen.getByRole("link", { name: "pbs_abc" }).getAttribute("href"),
    ).toBe("/browser-session/pbs_abc/stream");
    expect(
      screen.getByRole("link", { name: "bp_def" }).getAttribute("href"),
    ).toBe("/browser-profiles/bp_def");
  });

  test("renders only the id the run has", () => {
    renderStrip(makeRun({ browser_session_id: "pbs_abc" }));
    expect(screen.getByRole("link", { name: "pbs_abc" })).toBeTruthy();
    expect(screen.queryByTitle("Browser profile")).toBeNull();
  });

  test("renders no id chips at all when the run has neither", () => {
    // The run id itself lives in the top bar's "View Run" tab, not the strip.
    renderStrip(makeRun());
    expect(screen.queryByText("wr_123")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("groups the session and profile chips without the run id", () => {
    renderStrip(
      makeRun({
        browser_session_id: "pbs_abc",
        browser_profile_id: "bp_def",
      }),
    );
    expect(screen.queryByText("wr_123")).toBeNull();
    expect(screen.getByText("pbs_abc")).toBeTruthy();
    expect(screen.getByText("bp_def")).toBeTruthy();
  });
});

describe("RunSummaryStrip duration", () => {
  test("shows a single elapsed chip for a finalized run", () => {
    renderStrip(makeRun());
    expect(screen.getByText("Ran for 5m 0s")).toBeTruthy();
    expect(screen.queryByText(/^Started /)).toBeNull();
    expect(screen.queryByText(/^Finished /)).toBeNull();
  });

  test("hovering/focusing the chip breaks down all four run times", async () => {
    renderStrip(makeRun());
    fireEvent.focus(screen.getByText("Ran for 5m 0s"));
    const tooltip = await screen.findByRole("tooltip");
    const breakdown = within(tooltip);
    expect(breakdown.getByText(/Created /)).toBeTruthy();
    expect(breakdown.getByText(/Queued /)).toBeTruthy();
    expect(breakdown.getByText(/Started /)).toBeTruthy();
    expect(breakdown.getByText(/Finished /)).toBeTruthy();
  });

  test("shows started without an elapsed chip while the run is not finalized", () => {
    renderStrip(makeRun({ status: Status.Running }));
    expect(screen.getByText(/^Started /)).toBeTruthy();
    expect(screen.queryByText(/^Ran for /)).toBeNull();
  });

  test("a finalized run that never started falls back to the raw chips", () => {
    renderStrip(makeRun({ status: Status.Failed, started_at: null }));
    expect(screen.getByText(/^Finished /)).toBeTruthy();
    expect(screen.queryByText(/^Ran for /)).toBeNull();
  });

  test("renders no dates when the run has not started", () => {
    renderStrip(makeRun({ status: Status.Running, started_at: null }));
    expect(screen.queryByText(/^Started /)).toBeNull();
    expect(screen.queryByText(/^Ran for /)).toBeNull();
  });
});

describe("RunSummaryStrip status badge", () => {
  test("adopts the collapsible badge inside its own container", () => {
    const { container } = renderStrip(makeRun({ status: Status.Completed }));

    expect(
      container.querySelector('[class*="container-name:status"]'),
    ).not.toBeNull();
    // aria-label is only present in the badge's collapsible mode
    expect(container.querySelector('[aria-label="completed"]')).not.toBeNull();
  });
});
