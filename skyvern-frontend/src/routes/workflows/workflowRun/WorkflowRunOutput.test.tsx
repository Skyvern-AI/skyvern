// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
} from "../types/workflowRunTypes";
import { WorkflowRunOutput } from "./WorkflowRunOutput";

const mocks = vi.hoisted(() => ({
  activeItem: null as unknown,
  workflowRun: null as unknown,
  timeline: [] as unknown,
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
  }),
}));

vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isLoading: false,
  }),
}));

vi.mock("./useActiveWorkflowRunItem", () => ({
  useActiveWorkflowRunItem: () => [mocks.activeItem, vi.fn()],
}));

vi.mock("../components/CodeEditor", () => ({
  CodeEditor: ({ value }: { value: string }) => (
    <pre data-testid="code-editor">{value}</pre>
  ),
}));

vi.mock("@/components/SummarizeOutput", () => ({
  SummarizeOutput: () => null,
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ScrollAreaViewport: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "http_request",
    label: null,
    description: null,
    title: null,
    status: Status.Completed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

function buildBlockItem(block: WorkflowRunBlock): WorkflowRunTimelineBlockItem {
  return {
    type: "block",
    block,
    children: [],
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

function renderWorkflowRunOutput(activeBlock: WorkflowRunBlock) {
  mocks.activeItem = activeBlock;
  mocks.timeline = [buildBlockItem(activeBlock)];
  mocks.workflowRun = {
    workflow_run_id: activeBlock.workflow_run_id,
    workflow_title: "Demo workflow",
    outputs: {},
    downloaded_file_urls: [],
    downloaded_files: [],
  };

  return render(
    <MemoryRouter initialEntries={["/runs/demo?active=wrb_code"]}>
      <WorkflowRunOutput />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.activeItem = null;
  mocks.workflowRun = null;
  mocks.timeline = [];
});

afterEach(() => {
  cleanup();
});

describe("WorkflowRunOutput", () => {
  it("renders code block extracted information without the raw output wrapper", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code",
      block_type: "code",
      output: {
        extracted_information: {
          order_id: "ord_123",
          status: "shipped",
        },
        raw_code_output: "debug payload",
      },
    });

    renderWorkflowRunOutput(block);

    expect(screen.getByText("Extracted Information")).toBeDefined();
    expect(screen.getByText("order_id")).toBeDefined();
    expect(screen.getByText('"ord_123"')).toBeDefined();
    expect(screen.queryByText("raw_code_output")).toBeNull();
    expect(screen.queryByText('"debug payload"')).toBeNull();
  });

  it("keeps null code block extracted information in the extraction section", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_null_extraction",
      block_type: "code",
      output: {
        extracted_information: null,
        raw_code_output: "debug payload",
      },
    });

    renderWorkflowRunOutput(block);

    expect(screen.getByText("Extracted Information")).toBeDefined();
    expect(screen.getByText("null")).toBeDefined();
    expect(screen.queryByText("raw_code_output")).toBeNull();
    expect(screen.queryByText('"debug payload"')).toBeNull();
  });
});
