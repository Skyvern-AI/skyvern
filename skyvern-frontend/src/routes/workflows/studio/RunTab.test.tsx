// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test, vi } from "vitest";

import { RunTab } from "./RunTab";

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: [] }),
}));

vi.mock("./runview/RunView", () => ({
  RunView: (props: { onRetry?: () => void }) => (
    <div data-testid="runview" data-has-retry={props.onRetry ? "yes" : "no"} />
  ),
}));

afterEach(cleanup);

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
