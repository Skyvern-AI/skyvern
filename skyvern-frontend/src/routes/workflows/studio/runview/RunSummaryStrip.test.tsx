// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test } from "vitest";

import {
  Status,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";

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
      <RunSummaryStrip workflowRun={run} />
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

  test("renders the run id but no resource links when the run has neither", () => {
    renderStrip(makeRun());
    expect(screen.getByText("wr_123")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("groups the run id with the browser session and profile ids", () => {
    renderStrip(
      makeRun({
        browser_session_id: "pbs_abc",
        browser_profile_id: "bp_def",
      }),
    );
    expect(screen.getByText("wr_123")).toBeTruthy();
    expect(screen.getByText("pbs_abc")).toBeTruthy();
    expect(screen.getByText("bp_def")).toBeTruthy();
  });
});

describe("RunSummaryStrip visible dates", () => {
  test("shows started and finished as separate chips for a finalized run", () => {
    renderStrip(makeRun());
    expect(screen.getByText(/^Started /)).toBeTruthy();
    expect(screen.getByText(/^Finished /)).toBeTruthy();
  });

  test("shows started without finished while the run is not finalized", () => {
    renderStrip(makeRun({ status: Status.Running }));
    expect(screen.getByText(/^Started /)).toBeTruthy();
    expect(screen.queryByText(/^Finished /)).toBeNull();
  });

  test("renders no dates when the run has not started", () => {
    renderStrip(makeRun({ status: Status.Running, started_at: null }));
    expect(screen.queryByText(/^Started /)).toBeNull();
  });
});
