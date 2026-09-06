// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkflowRunRecording } from "./WorkflowRunRecording";

// The component only ever renders under the run-detail route, and the run id it
// finds there is what separates "still resolving" from "no run in view".
function renderUnderRunRoute() {
  return render(
    <MemoryRouter initialEntries={["/runs/wr_1"]}>
      <Routes>
        <Route path="/runs/:runId" element={<WorkflowRunRecording />} />
      </Routes>
    </MemoryRouter>,
  );
}

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  isLoading: false,
  isPlaceholderData: false,
}));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: mocks.isLoading,
    isPlaceholderData: mocks.isPlaceholderData,
  }),
}));

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.isLoading = false;
  mocks.isPlaceholderData = false;
});

describe("WorkflowRunRecording", () => {
  it("does not claim a run has no recording while its payload is still withheld", () => {
    mocks.isPlaceholderData = true;

    const { container } = renderUnderRunRoute();

    expect(container.textContent).not.toContain("No recording available");
  });

  it("states that a resolved run has no recording", () => {
    mocks.workflowRun = { workflow_run_id: "wr_1", recording_urls: [] };

    const { container } = renderUnderRunRoute();

    expect(container.textContent).toContain("No recording available");
  });
});
