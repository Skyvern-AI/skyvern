// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { WorkflowRun } from "../WorkflowRun";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
} from "../types/workflowRunTypes";
import { WorkflowRunOutput } from "./WorkflowRunOutput";

const mocks = vi.hoisted(() => ({
  workflowRun: null as unknown,
  timeline: [] as unknown,
  timelineIsPlaceholder: false,
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
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
    isPlaceholderData: mocks.timelineIsPlaceholder,
  }),
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

vi.mock("@/hooks/useApiCredential", () => ({
  useApiCredential: () => null,
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));
vi.mock("../hooks/useCacheKeyValuesQuery", () => ({
  useCacheKeyValuesQuery: () => ({ data: undefined }),
}));
vi.mock("../hooks/useBlockScriptsQuery", () => ({
  useBlockScriptsQuery: () => ({ data: undefined }),
}));
vi.mock("../hooks/useFallbackEpisodesQuery", () => ({
  useFallbackEpisodesQuery: () => ({ data: undefined }),
}));
vi.mock("../hooks/useRefreshOnboardingOnRunCompletion", () => ({
  useRefreshOnboardingOnRunCompletion: () => undefined,
}));
vi.mock("./useRunCompletionToast", () => ({
  useRunCompletionToast: () => undefined,
}));
vi.mock("./WorkflowRunTimeline", () => ({
  WorkflowRunTimeline: () => null,
}));
vi.mock("./WorkflowRunBlockDetail", () => ({
  WorkflowRunBlockDetail: () => null,
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
  mocks.timeline = [buildBlockItem(activeBlock)];
  mocks.workflowRun = {
    workflow_run_id: activeBlock.workflow_run_id,
    workflow_title: "Demo workflow",
    outputs: {},
    downloaded_file_urls: [],
    downloaded_files: [],
  };

  return render(
    <MemoryRouter
      initialEntries={[
        `/runs/demo?active=${activeBlock.workflow_run_block_id}`,
      ]}
    >
      <WorkflowRunOutput />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.workflowRun = null;
  mocks.timeline = [];
  mocks.timelineIsPlaceholder = false;
});

afterEach(() => {
  cleanup();
});

describe("WorkflowRunOutput", () => {
  it.each([
    { active: "", hasCurrentBlocks: true, expected: '"current output"' },
    { active: "", hasCurrentBlocks: false, expected: null },
    {
      active: "?active=wrb_cleanup_1",
      hasCurrentBlocks: true,
      expected: '"historical output"',
    },
  ])(
    "scopes default output to the current attempt ($active, $hasCurrentBlocks)",
    ({ active, hasCurrentBlocks, expected }) => {
      const historical = {
        ...buildBlockItem(
          buildBlock({
            workflow_run_block_id: "wrb_cleanup_1",
            label: "cleanup",
            block_type: "code",
            output: { extracted_information: "historical output" },
          }),
        ),
        attempt: 1,
      };
      const current = {
        ...buildBlockItem(
          buildBlock({
            workflow_run_block_id: "wrb_cleanup_2",
            label: "cleanup",
            block_type: "code",
            output: { extracted_information: "current output" },
          }),
        ),
        attempt: 2,
      };
      mocks.timeline = hasCurrentBlocks ? [historical, current] : [historical];
      mocks.workflowRun = {
        workflow_run_id: "wr_default",
        status: Status.Completed,
        attempt: 2,
        workflow: { workflow_definition: { finally_block_label: "cleanup" } },
      };

      render(
        <MemoryRouter initialEntries={[`/runs/wr_default${active}`]}>
          <WorkflowRunOutput />
        </MemoryRouter>,
      );

      expect(
        screen
          .queryAllByTestId("code-editor")
          .map((editor) => editor.textContent)
          .filter(Boolean),
      ).toEqual(expected ? [expected] : []);
    },
  );

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

describe("WorkflowRunOutput with a retained timeline", () => {
  it("renders no block output while the timeline still belongs to the previous run", () => {
    mocks.timelineIsPlaceholder = true;
    const block = buildBlock({
      workflow_run_block_id: "wrb_retained",
      block_type: "code",
      output: { extracted_information: { order_id: "ord_123" } },
    });

    const { container } = renderWorkflowRunOutput(block);

    expect(container.textContent).toBe("");
    expect(screen.queryByText('"ord_123"')).toBeNull();
  });
});

describe("legacy WorkflowRun failure card", () => {
  it("uses the final attempt's code details and finally-block outcome", () => {
    const reason = "CodeBlock failed with ValueError at line 8";
    const body = buildBlock({
      block_type: "code",
      label: "body",
      status: Status.Failed,
      failure_reason: reason,
    });
    const finallyBlock = buildBlock({ label: "cleanup" });
    mocks.workflowRun = {
      workflow_run_id: "wr_retry",
      status: Status.Failed,
      attempt: 2,
      failure_reason: reason,
      task_v2: null,
      workflow: {
        title: "Retry workflow",
        workflow_permanent_id: "wpid_retry",
        workflow_definition: { blocks: [], finally_block_label: "cleanup" },
      },
    };
    mocks.timeline = [
      {
        ...buildBlockItem({
          ...body,
          workflow_run_block_id: "wrb_body_1",
          error_codes: ["runner_unavailable"],
        }),
        attempt: 1,
      },
      {
        ...buildBlockItem({
          ...finallyBlock,
          workflow_run_block_id: "wrb_cleanup_1",
          status: Status.Completed,
        }),
        attempt: 1,
      },
      {
        ...buildBlockItem({
          ...body,
          workflow_run_block_id: "wrb_body_2",
          error_codes: ["user_code_error"],
        }),
        attempt: 2,
      },
      {
        ...buildBlockItem({
          ...finallyBlock,
          workflow_run_block_id: "wrb_cleanup_2",
          status: Status.Failed,
        }),
        attempt: 2,
      },
    ];

    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/runs/wr_retry?embed=true"]}>
          <WorkflowRun />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      screen.getByText('"Execute on any outcome" block (cleanup) failed.'),
    ).toBeDefined();
    expect(
      screen.getByText("The block's code raised ValueError"),
    ).toBeDefined();
    expect(screen.queryByText(/completed successfully/)).toBeNull();
    expect(screen.queryByText("runner_unavailable")).toBeNull();
  });
});
