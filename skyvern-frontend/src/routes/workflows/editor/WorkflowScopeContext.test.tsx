import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import {
  WorkflowScopeContext,
  useWorkflowScopeId,
} from "./WorkflowScopeContext";

afterEach(cleanup);

function Probe() {
  const id = useWorkflowScopeId();
  return <div data-testid="id">{id ?? "<none>"}</div>;
}

describe("WorkflowScopeContext", () => {
  test("returns null when no provider", () => {
    render(<Probe />);
    expect(screen.getByTestId("id").textContent).toBe("<none>");
  });

  test("returns provided id", () => {
    render(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_abc123", readOnly: false }}
      >
        <Probe />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("id").textContent).toBe("wf_abc123");
  });
});
