// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
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
    ...overrides,
  } as WorkflowRunStatusApiResponseWithWorkflow;
}

function renderStrip(
  run: WorkflowRunStatusApiResponseWithWorkflow,
  liveElapsed: string | null = null,
) {
  return render(
    // The studio shell provides this in production (StudioShell root).
    <TooltipProvider delayDuration={0}>
      <RunSummaryStrip
        workflowRun={run}
        timeline={undefined}
        liveElapsed={liveElapsed}
      />
    </TooltipProvider>,
  );
}

// The numeral is its own emphasized span inside the focusable chip, so match
// the chip (the tab stop) by its full text.
const durationChip = (text: string) =>
  screen.getByText(
    (_, node) =>
      node?.getAttribute("tabindex") === "0" && node.textContent === text,
  );

describe("RunSummaryStrip duration", () => {
  test("shows a single elapsed chip for a finalized run", () => {
    renderStrip(makeRun());
    expect(durationChip("Ran for 5m 0s")).toBeTruthy();
    expect(screen.queryByText(/^Started /)).toBeNull();
    expect(screen.queryByText(/^Finished /)).toBeNull();
  });

  test("hovering/focusing the chip breaks down all four run times", async () => {
    renderStrip(makeRun());
    fireEvent.focus(durationChip("Ran for 5m 0s"));
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

  test("a live run shows the ticking elapsed in the chip's place instead of the start date", () => {
    renderStrip(makeRun({ status: Status.Running }), "2m 14s");
    expect(durationChip("2m 14s")).toBeTruthy();
    expect(screen.queryByText(/^Started /)).toBeNull();
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

  test("renders no links — ids live in the top bar and the Inputs view", () => {
    renderStrip(makeRun());
    expect(screen.queryByRole("link")).toBeNull();
  });
});
