// @vitest-environment jsdom

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import type { DebugSessionRun } from "../../hooks/useDebugSessionRunsQuery";
import { getRunActivityKey } from "./runActivity";
import { RecentActivityList } from "./RecentActivityList";

function makeRun(partial: Partial<DebugSessionRun> = {}): DebugSessionRun {
  return {
    ai_fallback: null,
    block_label: "Login",
    browser_session_id: "bs_1",
    code_gen: null,
    debug_session_id: "ds_1",
    failure_reason: null,
    output_parameter_id: "op_1",
    run_with: "agent",
    script_run_id: null,
    status: "completed",
    workflow_id: "w_1",
    workflow_permanent_id: "wpid_1",
    workflow_run_id: "wr_1",
    created_at: "2026-06-15T10:00:00Z",
    queued_at: null,
    started_at: null,
    finished_at: null,
    ...partial,
  };
}

function renderList(isWorkflowRunning: boolean) {
  const onSelect = vi.fn();
  const { container } = render(
    <RecentActivityList
      runs={[makeRun()]}
      currentActivityKey={null}
      isWorkflowRunning={isWorkflowRunning}
      blockTypeByLabel={new Map()}
      now={Date.parse("2026-06-15T10:05:00Z")}
      onSelect={onSelect}
    />,
  );
  const row = container.querySelector("button") as HTMLButtonElement;
  return { onSelect, row };
}

describe("RecentActivityList", () => {
  test("navigates when no workflow run is in progress", () => {
    const { onSelect, row } = renderList(false);
    expect(row.getAttribute("aria-disabled")).toBeNull();
    fireEvent.click(row);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  test("disables run selection while a workflow run is in progress", () => {
    const { onSelect, row } = renderList(true);
    expect(row.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(row);
    expect(onSelect).not.toHaveBeenCalled();
  });

  test("tracks the current row by workflow run and block label", () => {
    const loginRun = makeRun({ workflow_run_id: "wr_1", block_label: "Login" });
    const extractRun = makeRun({
      workflow_run_id: "wr_1",
      block_label: "Extract",
    });
    const { container } = render(
      <RecentActivityList
        runs={[loginRun, extractRun]}
        currentActivityKey={getRunActivityKey(extractRun)}
        isWorkflowRunning={false}
        blockTypeByLabel={new Map()}
        now={Date.parse("2026-06-15T10:05:00Z")}
        onSelect={vi.fn()}
      />,
    );

    const rows = Array.from(container.querySelectorAll("button"));
    expect(rows).toHaveLength(2);
    const [newestRow, oldestRow] = rows;
    if (!newestRow || !oldestRow) {
      throw new Error("Expected recent activity rows to render");
    }
    expect(newestRow.textContent).toContain("Extract");
    expect(newestRow.getAttribute("aria-current")).toBe("true");
    expect(oldestRow.textContent).toContain("Login");
    expect(oldestRow.getAttribute("aria-current")).toBeNull();
  });
});
