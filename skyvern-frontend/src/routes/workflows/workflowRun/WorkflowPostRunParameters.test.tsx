// @vitest-environment jsdom

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
  }),
}));

vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: [],
    isLoading: false,
  }),
}));

vi.mock("./useActiveWorkflowRunItem", () => ({
  useActiveWorkflowRunItem: () => [null, vi.fn()],
}));

vi.mock("@/components/KeyValueInput", () => ({
  KeyValueInput: ({ value }: { value: string | null }) => (
    <div data-testid="key-value-input">{value}</div>
  ),
}));

vi.mock("@/components/ProxySelector", () => ({
  ProxySelector: () => <div data-testid="proxy-selector" />,
}));

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProxyLocation } from "@/api/types";

import { WorkflowPostRunParameters } from "./WorkflowPostRunParameters";

const mocks = vi.hoisted(() => ({
  workflowRun: null as unknown,
}));

function buildWorkflowRun(extraHttpHeaders: Record<string, string> | null) {
  return {
    workflow_run_id: "wr_1",
    parameters: {},
    workflow: {
      workflow_definition: {
        blocks: [],
        parameters: [],
      },
      run_with: null,
    },
    task_v2: null,
    webhook_callback_url: null,
    proxy_location: ProxyLocation.Residential,
    extra_http_headers: extraHttpHeaders,
    browser_session_id: null,
    run_with: null,
    max_screenshot_scrolls: null,
  };
}

beforeEach(() => {
  mocks.workflowRun = buildWorkflowRun(null);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkflowPostRunParameters extra HTTP headers", () => {
  it("hides default empty extra HTTP headers", () => {
    mocks.workflowRun = buildWorkflowRun({});

    render(<WorkflowPostRunParameters />);

    expect(screen.getByText("Other Agent Inputs")).toBeDefined();
    expect(screen.queryByText("Extra HTTP Headers")).toBeNull();
    expect(screen.queryByTestId("key-value-input")).toBeNull();
  });

  it("shows non-empty extra HTTP headers", () => {
    mocks.workflowRun = buildWorkflowRun({ "X-Trace-Id": "abc123" });

    render(<WorkflowPostRunParameters />);

    expect(screen.getByText("Extra HTTP Headers")).toBeDefined();
    expect(screen.getByTestId("key-value-input").textContent).toBe(
      '{"X-Trace-Id":"abc123"}',
    );
  });
});
