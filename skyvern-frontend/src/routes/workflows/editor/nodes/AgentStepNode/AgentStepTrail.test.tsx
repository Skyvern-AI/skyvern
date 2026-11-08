// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import type { AgentStepTrailView } from "@/routes/workflows/studio/useAgentStepTrail";
import type { WorkflowRunTimelineBlockItem } from "@/routes/workflows/types/workflowRunTypes";
import { useRunViewStore } from "@/store/RunViewStore";

import type { AppNode } from "..";
import { getWorkflowBlocks } from "../../workflowEditorUtils";
import { AgentStepTrail } from "./AgentStepTrail";
import { type AgentStepNode, agentStepNodeDefaultData } from "./types";

const trail: { current: AgentStepTrailView | null } = { current: null };

vi.mock("@/routes/workflows/studio/useAgentStepTrail", async () => {
  const actual = await vi.importActual<
    typeof import("@/routes/workflows/studio/useAgentStepTrail")
  >("@/routes/workflows/studio/useAgentStepTrail");
  return { ...actual, useAgentStepTrail: () => trail.current };
});

const agentStepNode: AgentStepNode = {
  id: "agent-node",
  type: "agentStep",
  position: { x: 0, y: 0 },
  data: { ...agentStepNodeDefaultData, label: "agent", prompt: "Find it" },
};

const childRow = {
  type: "block",
  thought: null,
  children: [],
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  block: {
    workflow_run_block_id: "wrb_child",
    block_type: "task",
    label: "agent_task_1",
    status: Status.Running,
  },
} as unknown as WorkflowRunTimelineBlockItem;

function stepWithCalls(): AgentStepTrailView {
  const item = {
    ...childRow,
    block: {
      ...childRow.block,
      workflow_run_block_id: "wrb_agent",
      block_type: "agent_step",
      label: "agent",
      actions: [],
    },
  } as unknown as WorkflowRunTimelineBlockItem;
  return {
    item,
    entries: [
      {
        call: {
          index: 1,
          tool: "browser_task",
          block_type: "task",
          label: "agent_task_1",
          status: Status.Running,
          started_at: "2026-01-01T00:00:00+00:00",
          arguments: '{"navigation_goal": "Read the price"}',
        },
        child: childRow,
        running: true,
        status: Status.Running,
      },
    ],
  };
}

let reactFlow: ReturnType<typeof useReactFlow<AppNode>> | null = null;

function CaptureFlow() {
  reactFlow = useReactFlow<AppNode>();
  return null;
}

function renderTrail() {
  return render(
    <MemoryRouter initialEntries={["/?wr=wr_1"]}>
      <ReactFlowProvider initialNodes={[agentStepNode]}>
        <CaptureFlow />
        <AgentStepTrail agentStepLabel="agent" />
      </ReactFlowProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  trail.current = null;
  useRunViewStore.getState().reset();
});

describe("AgentStepTrail", () => {
  test("draws each call beside the step without adding a node to the workflow", () => {
    trail.current = stepWithCalls();
    renderTrail();
    const blocksBefore = getWorkflowBlocks(reactFlow!.getNodes(), []);

    fireEvent.click(screen.getByText("#1 Browser Task"));

    expect(screen.getByText("Read the price")).toBeTruthy();
    expect(reactFlow!.getNodes().map((node) => node.id)).toEqual([
      "agent-node",
    ]);
    expect(getWorkflowBlocks(reactFlow!.getNodes(), [])).toEqual(blocksBefore);
    expect(useRunViewStore.getState().selectedAgentStepCall).toEqual({
      runId: "wr_1",
      workflowRunBlockId: "wrb_agent",
      index: 1,
    });
    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_child");
  });

  test("any later pin of another frame clears the selected call", () => {
    trail.current = stepWithCalls();
    renderTrail();
    const card = () => screen.getByText("#1 Browser Task").closest("button");

    fireEvent.click(screen.getByText("#1 Browser Task"));
    expect(card()?.getAttribute("aria-pressed")).toBe("true");

    act(() => useRunViewStore.getState().pinFrame("act_elsewhere"));

    expect(card()?.getAttribute("aria-pressed")).toBe("false");
    expect(useRunViewStore.getState().selectedAgentStepCall).toBeNull();
  });

  test("a run with no call list draws nothing", () => {
    renderTrail();

    expect(screen.queryByTestId("agent-step-trail")).toBeNull();
  });
});
