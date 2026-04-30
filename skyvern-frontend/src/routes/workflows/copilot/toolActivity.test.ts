import { describe, expect, it } from "vitest";

import { ToolActivity, applyToolCall, applyToolResult } from "./toolActivity";
import {
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
} from "./workflowCopilotTypes";

const make = (
  overrides: Partial<ToolActivity> & Pick<ToolActivity, "tool_call_id">,
): ToolActivity => ({
  tool_name: "update_workflow",
  status: "running",
  ...overrides,
});

const result = (
  overrides: Partial<WorkflowCopilotToolResultUpdate> &
    Pick<WorkflowCopilotToolResultUpdate, "tool_call_id" | "success">,
): WorkflowCopilotToolResultUpdate => ({
  type: "tool_result",
  tool_name: "update_workflow",
  summary: "",
  iteration: 0,
  ...overrides,
});

const call = (
  overrides: Partial<WorkflowCopilotToolCallUpdate> &
    Pick<WorkflowCopilotToolCallUpdate, "tool_call_id">,
): WorkflowCopilotToolCallUpdate => ({
  type: "tool_call",
  tool_name: "update_workflow",
  tool_input: {},
  iteration: 0,
  ...overrides,
});

describe("applyToolResult", () => {
  it("returns prev unchanged when tool_call_id has no matching entry", () => {
    const prev = [make({ tool_call_id: "c1" })];
    const next = applyToolResult(
      prev,
      result({ tool_call_id: "missing", success: true, summary: "ok" }),
    );
    expect(next).toBe(prev);
  });

  it("flips a running entry to error and copies detail", () => {
    const prev = [make({ tool_call_id: "c1" })];
    const next = applyToolResult(
      prev,
      result({
        tool_call_id: "c1",
        success: false,
        summary: "Failed: short",
        detail: "long sanitized error text for tooltip",
      }),
    );
    expect(next[0]!).toMatchObject({
      status: "error",
      summary: "Failed: short",
      detail: "long sanitized error text for tooltip",
    });
  });

  it("normalizes payload null detail to undefined on the activity", () => {
    const prev = [make({ tool_call_id: "c1" })];
    const next = applyToolResult(
      prev,
      result({
        tool_call_id: "c1",
        success: false,
        summary: "x",
        detail: null,
      }),
    );
    expect(next[0]!.detail).toBeUndefined();
  });

  it("links an adjacent failure to the next same-tool success", () => {
    const prev: ToolActivity[] = [
      make({
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
        summary: "Failed: bad yaml",
      }),
      make({
        tool_name: "update_workflow",
        tool_call_id: "c2",
        status: "running",
      }),
    ];
    const next = applyToolResult(
      prev,
      result({
        tool_call_id: "c2",
        success: true,
        summary: "Workflow updated",
      }),
    );
    expect(next[0]!).toMatchObject({
      status: "error",
      recovered: true,
      linkedRecovery: true,
    });
    expect(next[1]!).toMatchObject({
      status: "success",
      linkedRecovery: true,
    });
  });

  it("recovers but does NOT link a non-adjacent prior failure", () => {
    const prev: ToolActivity[] = [
      make({
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
      }),
      make({
        tool_name: "get_block_schema",
        tool_call_id: "c2",
        status: "success",
      }),
      make({
        tool_name: "update_workflow",
        tool_call_id: "c3",
        status: "running",
      }),
    ];
    const next = applyToolResult(
      prev,
      result({ tool_call_id: "c3", success: true, summary: "ok" }),
    );
    expect(next[0]!).toMatchObject({
      status: "error",
      recovered: true,
    });
    expect(next[0]!.linkedRecovery).toBeFalsy();
    expect(next[2]!.linkedRecovery).toBeFalsy();
    expect(next[2]!.status).toBe("success");
    expect(next[1]!).toEqual(prev[1]!);
  });

  it("recovers ALL prior unrecovered same-tool failures, not just one", () => {
    const prev: ToolActivity[] = [
      make({
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
      }),
      make({
        tool_name: "update_workflow",
        tool_call_id: "c2",
        status: "error",
      }),
      make({
        tool_name: "update_workflow",
        tool_call_id: "c3",
        status: "running",
      }),
    ];
    const next = applyToolResult(
      prev,
      result({ tool_call_id: "c3", success: true, summary: "ok" }),
    );
    expect(next[0]!.recovered).toBe(true);
    expect(next[1]!.recovered).toBe(true);
    expect(next[0]!.linkedRecovery).toBeFalsy();
    expect(next[1]!.linkedRecovery).toBe(true);
    expect(next[2]!).toMatchObject({
      linkedRecovery: true,
    });
  });

  it("does not re-recover a failure that an earlier intermediate success already resolved", () => {
    const prev: ToolActivity[] = [
      {
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
        recovered: true,
        linkedRecovery: true,
      },
      {
        tool_name: "update_workflow",
        tool_call_id: "c2",
        status: "success",
        linkedRecovery: true,
      },
      {
        tool_name: "update_workflow",
        tool_call_id: "c3",
        status: "error",
      },
      {
        tool_name: "update_workflow",
        tool_call_id: "c4",
        status: "running",
      },
    ];
    const next = applyToolResult(
      prev,
      result({ tool_call_id: "c4", success: true, summary: "ok" }),
    );
    expect(next[0]).toBe(prev[0]);
    expect(next[1]).toBe(prev[1]);
    expect(next[2]!).toMatchObject({
      recovered: true,
      linkedRecovery: true,
    });
    expect(next[3]!).toMatchObject({
      status: "success",
      linkedRecovery: true,
    });
  });

  it("links the adjacent retry success after applyToolCall has already marked the prior failure recovered", () => {
    // Live flow: tool_call appends running and clears the pulse on the
    // prior failure (recovered=true). Then tool_result success arrives and
    // must still draw the connector on the adjacent pair.
    let state: ToolActivity[] = [
      {
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
      },
    ];
    state = applyToolCall(
      state,
      call({ tool_name: "update_workflow", tool_call_id: "c2" }),
    );
    expect(state[0]!.recovered).toBe(true);
    expect(state[0]!.linkedRecovery).toBeUndefined();

    state = applyToolResult(
      state,
      result({ tool_call_id: "c2", success: true, summary: "ok" }),
    );
    expect(state[0]!).toMatchObject({
      status: "error",
      recovered: true,
      linkedRecovery: true,
    });
    expect(state[1]!).toMatchObject({
      status: "success",
      linkedRecovery: true,
    });
  });

  it("does not link to a different tool's failure", () => {
    const prev: ToolActivity[] = [
      make({
        tool_name: "click",
        tool_call_id: "c1",
        status: "error",
      }),
      make({
        tool_name: "update_workflow",
        tool_call_id: "c2",
        status: "running",
      }),
    ];
    const next = applyToolResult(
      prev,
      result({
        tool_name: "update_workflow",
        tool_call_id: "c2",
        success: true,
        summary: "ok",
      }),
    );
    // Different tool: not linked, but the click failure stays unrecovered
    // until a tool_call event clears it (handled by applyToolCall).
    expect(next[0]!.recovered).toBeUndefined();
    expect(next[1]!.linkedRecovery).toBeFalsy();
  });
});

describe("applyToolCall", () => {
  it("appends a running entry", () => {
    const prev: ToolActivity[] = [];
    const next = applyToolCall(
      prev,
      call({ tool_name: "update_workflow", tool_call_id: "c1" }),
    );
    expect(next).toHaveLength(1);
    expect(next[0]!).toMatchObject({
      tool_name: "update_workflow",
      tool_call_id: "c1",
      status: "running",
    });
  });

  it("stops the pulse on prior unrecovered failures of any tool", () => {
    const prev: ToolActivity[] = [
      {
        tool_name: "update_workflow",
        tool_call_id: "c1",
        status: "error",
      },
      {
        tool_name: "evaluate",
        tool_call_id: "c2",
        status: "success",
      },
    ];
    const next = applyToolCall(
      prev,
      call({ tool_name: "evaluate", tool_call_id: "c3" }),
    );
    expect(next[0]!.recovered).toBe(true);
    expect(next[0]!.linkedRecovery).toBeUndefined();
    expect(next[1]).toEqual(prev[1]);
    expect(next[2]!).toMatchObject({
      tool_call_id: "c3",
      status: "running",
    });
  });

  it("leaves already-recovered failures untouched", () => {
    const prior: ToolActivity = {
      tool_name: "update_workflow",
      tool_call_id: "c1",
      status: "error",
      recovered: true,
      linkedRecovery: true,
    };
    const prev: ToolActivity[] = [prior];
    const next = applyToolCall(
      prev,
      call({ tool_name: "evaluate", tool_call_id: "c2" }),
    );
    expect(next[0]!).toBe(prior);
  });
});
