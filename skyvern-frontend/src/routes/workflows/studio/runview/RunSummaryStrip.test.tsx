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
      <RunSummaryStrip workflowRun={run} elapsed="5m" />
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

  test("renders no meta line when the run has neither", () => {
    renderStrip(makeRun());
    expect(screen.queryByRole("link")).toBeNull();
  });
});
