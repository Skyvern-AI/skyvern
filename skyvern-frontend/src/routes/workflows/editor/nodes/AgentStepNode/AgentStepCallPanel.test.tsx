// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { Status } from "@/api/types";

import {
  type AgentStepTrailView,
  useSelectAgentStepCall,
} from "@/routes/workflows/studio/useAgentStepTrail";
import type { WorkflowRunTimelineBlockItem } from "@/routes/workflows/types/workflowRunTypes";
import { useRunViewStore } from "@/store/RunViewStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { AgentStepCallPanel } from "./AgentStepCallPanel";

const trail: { current: AgentStepTrailView | null } = { current: null };

vi.mock("@/routes/workflows/studio/useAgentStepTrail", async () => {
  const actual = await vi.importActual<
    typeof import("@/routes/workflows/studio/useAgentStepTrail")
  >("@/routes/workflows/studio/useAgentStepTrail");
  return { ...actual, useAgentStepTrail: () => trail.current };
});

afterEach(() => {
  cleanup();
  trail.current = null;
  useRunViewStore.getState().reset();
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
  useWorkflowPanelStore.getState().closeWorkflowPanel();
});

function renderPanel(runId: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[`/?wr=${runId}`]}>
        <AgentStepCallPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("a browser task call shows the Task form read-only, filled from the block it ran", async () => {
  const block = {
    block_type: "task",
    label: "agent_task_1",
    url: "https://example.com/",
    navigation_goal: "Read the price",
    data_schema: { type: "object", properties: { price: { type: "string" } } },
    engine: "skyvern-3.0",
  };
  trail.current = {
    item: {} as WorkflowRunTimelineBlockItem,
    entries: [
      {
        call: {
          index: 1,
          tool: "browser_task",
          block_type: "task",
          label: "agent_task_1",
          status: Status.Completed,
          started_at: "2026-01-01T00:00:00+00:00",
          arguments: "{}",
          block: JSON.stringify(block),
        },
        child: null,
        running: false,
        status: Status.Completed,
      },
    ],
  };
  useRunViewStore.getState().pinFrame("wrb_agent", null, {
    runId: "wr_1",
    workflowRunBlockId: "wrb_agent",
    index: 1,
  });

  renderPanel("wr_1");

  const panel = screen.getByRole("region", { name: "#1 Browser Task" });
  expect(document.activeElement).toBe(panel);
  expect(screen.getByLabelText("Copy block YAML")).toBeTruthy();
  expect(panel.textContent).toContain("Read the price");
  expect(panel.querySelector("[inert], fieldset[disabled]")).toBeNull();

  const goal = screen.getByDisplayValue("Read the price");
  expect(goal.getAttribute("aria-readonly")).toBe("true");
  expect((goal as HTMLTextAreaElement | HTMLInputElement).readOnly).toBe(true);
  for (const inputType of [
    "insertText",
    "deleteContentBackward",
    "insertFromPaste",
  ]) {
    const edit = new InputEvent("beforeinput", {
      inputType,
      bubbles: true,
      cancelable: true,
    });
    goal.dispatchEvent(edit);
    expect(edit.defaultPrevented).toBe(true);
  }

  expect(panel.textContent).toContain("Read only");
  const advanced = screen.getByRole("button", { name: /Advanced Settings/ });
  expect(advanced.getAttribute("aria-disabled")).toBeNull();
  fireEvent.click(advanced);
  expect(advanced.getAttribute("aria-expanded")).toBe("true");
  await waitFor(() =>
    expect(
      panel.querySelector("[role=switch]")?.getAttribute("aria-disabled"),
    ).toBe("true"),
  );

  fireEvent.keyDown(goal, { key: "Escape" });
  expect(useRunViewStore.getState().selectedAgentStepCall).toBeNull();
});

test("a send_email call whose YAML omits file_attachments renders its form instead of crashing", () => {
  const block = {
    block_type: "send_email",
    label: "agent_send_email_2",
    recipients: ["ops@example.com"],
    subject: "Weekly totals",
    body: "See attached totals.",
  };
  trail.current = {
    item: {} as WorkflowRunTimelineBlockItem,
    entries: [
      {
        call: {
          index: 2,
          tool: "run_block",
          block_type: "send_email",
          label: "agent_send_email_2",
          status: Status.Completed,
          started_at: "2026-01-01T00:00:00+00:00",
          arguments: "{}",
          block: JSON.stringify(block),
        },
        child: null,
        running: false,
        status: Status.Completed,
      },
    ],
  };
  useRunViewStore.getState().pinFrame("wrb_agent", null, {
    runId: "wr_1",
    workflowRunBlockId: "wrb_agent",
    index: 2,
  });

  renderPanel("wr_1");

  expect(screen.queryByLabelText("agent-step-call panel error")).toBeNull();
  expect(
    screen.getByRole("region", { name: "#2 Send Email" }).textContent,
  ).toContain("See attached totals.");
});

test("a call selected on another run shows no panel", () => {
  trail.current = {
    item: {} as WorkflowRunTimelineBlockItem,
    entries: [
      {
        call: {
          index: 1,
          tool: "read_run_data",
          status: Status.Completed,
          started_at: "2026-01-01T00:00:00+00:00",
          arguments: "{}",
        },
        child: null,
        running: false,
        status: Status.Completed,
      },
    ],
  };
  useRunViewStore.getState().pinFrame("wrb_agent", null, {
    runId: "wr_1",
    workflowRunBlockId: "wrb_agent",
    index: 1,
  });

  renderPanel("wr_2");

  expect(screen.queryByText("#1 Read run data")).toBeNull();
});

test("a selected call and a selected block never show their panels at the same time", () => {
  const entry = {
    call: {
      index: 1,
      tool: "read_run_data",
      status: Status.Completed,
      started_at: "2026-01-01T00:00:00+00:00",
      arguments: "{}",
    },
    child: null,
    running: false,
    status: Status.Completed,
  };
  trail.current = {
    item: {} as WorkflowRunTimelineBlockItem,
    entries: [entry],
  };
  useWorkflowPanelStore.getState().setSelectedBlockId("block_node");
  const { result } = renderHook(() => useSelectAgentStepCall(), {
    wrapper: ({ children }) => (
      <MemoryRouter initialEntries={["/?wr=wr_1"]}>{children}</MemoryRouter>
    ),
  });

  act(() =>
    result.current(
      {
        workflow_run_block_id: "wrb_agent",
      } as WorkflowRunTimelineBlockItem["block"],
      entry,
    ),
  );
  expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();
  renderPanel("wr_1");
  expect(screen.getByRole("region", { name: "#1 Read run data" })).toBeTruthy();

  act(() => useWorkflowPanelStore.getState().setSelectedBlockId("block_node"));
  expect(screen.queryByRole("region", { name: "#1 Read run data" })).toBeNull();
  expect(useRunViewStore.getState().selectedAgentStepCall).toBeNull();

  act(() => {
    useWorkflowPanelStore.getState().setSelectedBlockId(null);
    result.current(
      {
        workflow_run_block_id: "wrb_agent",
      } as WorkflowRunTimelineBlockItem["block"],
      entry,
    );
  });
  expect(screen.getByRole("region", { name: "#1 Read run data" })).toBeTruthy();
  act(() =>
    useWorkflowPanelStore
      .getState()
      .setWorkflowPanelState({ active: true, content: "nodeLibrary" }),
  );
  expect(screen.queryByRole("region", { name: "#1 Read run data" })).toBeNull();
  expect(useRunViewStore.getState().selectedAgentStepCall).toBeNull();
});
