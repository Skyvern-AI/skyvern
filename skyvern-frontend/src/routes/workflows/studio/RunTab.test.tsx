// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { RunTab } from "./RunTab";

const { runsQueryMock } = vi.hoisted(() => ({ runsQueryMock: vi.fn() }));

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));

vi.mock("./runview/RunView", () => ({
  RunView: (props: { onRetry?: () => void; runIdPending?: boolean }) => (
    <div
      data-testid="runview"
      data-has-retry={props.onRetry ? "yes" : "no"}
      data-run-id-pending={props.runIdPending ? "yes" : "no"}
    />
  ),
}));

afterEach(cleanup);
beforeEach(() => runsQueryMock.mockReturnValue({ data: [], isPending: false }));

// useStudioRunId reads ?wr= from the router, so the MemoryRouter URL drives it
// (and ?bl=, which RunTab reads directly) — no hook mock needed.
function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RunTab />
    </MemoryRouter>,
  );
}

describe("RunTab block-scoped retry", () => {
  test("suppresses the retry CTA for a block run (?bl= present)", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=Block%201");
    expect(screen.getByTestId("runview").getAttribute("data-has-retry")).toBe(
      "no",
    );
  });

  test("wires the retry CTA for a full run (no ?bl=)", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1");
    expect(screen.getByTestId("runview").getAttribute("data-has-retry")).toBe(
      "yes",
    );
  });
});

describe("RunTab run-id resolution", () => {
  test("marks the run id pending while the recent-runs query is loading", () => {
    runsQueryMock.mockReturnValue({ data: undefined, isPending: true });
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("yes");
  });

  test("does not mark pending once the recent-runs query settles empty", () => {
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("no");
  });

  test("does not mark pending when the run id comes from the URL", () => {
    runsQueryMock.mockReturnValue({ data: undefined, isPending: true });
    renderAt("/workflows/wpid_abc/studio?wr=run_1");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("no");
  });

  test("marks the run id pending while the globalWorkflows prerequisite is still loading (query disabled)", () => {
    runsQueryMock.mockReturnValue({
      data: undefined,
      isPending: true,
      isLoading: false,
    });
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("yes");
  });
});
