// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { WorkflowTriggerNode } from "../../nodes/WorkflowTriggerNode/types";

const mockNodes = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeData = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodes.get(id),
      updateNodeData,
    }),
    // WorkflowTriggerEditor (nested under the form) subscribes via
    // useNodesData; mirror the same lookup so the fixture covers both
    // reads.
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      if (!node) return null;
      return { id: node.id, type: node.type, data: node.data };
    },
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
  };
});

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return {
    ...actual,
    useParams: () => ({ workflowPermanentId: "wpid_parent" }),
  };
});

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

const beginInternalUpdate = vi.fn();
const endInternalUpdate = vi.fn();

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({
    beginInternalUpdate,
    endInternalUpdate,
  }),
}));

const useTargetWorkflowParametersQueryMock = vi.fn<
  (id: string) => {
    workflowParameters: Array<unknown>;
    isLoading: boolean;
    isError: boolean;
    workflowTitle: string;
  }
>(() => ({
  workflowParameters: [] as Array<unknown>,
  isLoading: false,
  isError: false,
  workflowTitle: "",
}));

vi.mock(
  "../../nodes/WorkflowTriggerNode/useTargetWorkflowParametersQuery",
  () => ({
    useTargetWorkflowParametersQuery: (id: string) =>
      useTargetWorkflowParametersQueryMock(id),
  }),
);

vi.mock("../../nodes/WorkflowTriggerNode/WorkflowSelector", () => ({
  WorkflowSelector: (props: {
    nodeId: string;
    value: string;
    onChange: (value: string) => void;
    workflowTitle: string;
    onTitleChange: (title: string) => void;
    excludeWorkflowPermanentId?: string;
  }) => (
    <div data-testid="workflow-selector">
      <span data-testid="workflow-selector-value">{props.value}</span>
      <span data-testid="workflow-selector-title">{props.workflowTitle}</span>
      <span data-testid="workflow-selector-exclude">
        {props.excludeWorkflowPermanentId ?? ""}
      </span>
      <button
        data-testid="workflow-selector-change"
        onClick={() => props.onChange("wpid_other")}
      />
      <button
        data-testid="workflow-selector-change-same"
        onClick={() => props.onChange(props.value)}
      />
      <button
        data-testid="workflow-selector-title-change"
        onClick={() => props.onTitleChange("My Workflow")}
      />
    </div>
  ),
}));

vi.mock("../../nodes/WorkflowTriggerNode/BrowserSessionSelector", () => ({
  BrowserSessionSelector: (props: {
    value: string;
    onChange: (value: string) => void;
    waitForCompletion: boolean;
  }) => (
    <div
      data-testid="browser-session-selector"
      data-wait-for-completion={String(props.waitForCompletion)}
    >
      <span data-testid="browser-session-value">{props.value}</span>
      <button
        data-testid="browser-session-parent"
        onClick={() => props.onChange("__parent__")}
      />
      <button
        data-testid="browser-session-fresh"
        onClick={() => props.onChange("__fresh__")}
      />
      <button
        data-testid="browser-session-custom"
        onClick={() => props.onChange("session_abc")}
      />
    </div>
  ),
}));

vi.mock("../../nodes/WorkflowTriggerNode/PayloadParameterFields", () => ({
  PayloadParameterFields: (props: {
    payload: string;
    onChange: (value: string) => void;
    nodeId: string;
    isLoading: boolean;
  }) => (
    <div
      data-testid="payload-parameter-fields"
      data-loading={String(props.isLoading)}
    >
      <span data-testid="payload-value">{props.payload}</span>
      <button
        data-testid="payload-change"
        onClick={() => props.onChange('{"foo":"bar"}')}
      />
    </div>
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      data-testid={`wbi-ph-${props.placeholder}`}
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: (props: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
    availableOutputParameters: Array<string>;
  }) => (
    <div data-testid="parameters-multi-select">
      <button
        data-testid="parameters-change"
        onClick={() => props.onParametersChange(["p1"])}
      />
    </div>
  ),
}));

vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: (props: {
    continueOnFailure: boolean;
    nextLoopOnFailure?: boolean;
    blockType: string;
    isInsideForLoop: boolean;
    hideTopSeparator?: boolean;
    onContinueOnFailureChange: (checked: boolean) => void;
    onNextLoopOnFailureChange: (checked: boolean) => void;
  }) => (
    <div
      data-testid="block-execution-options"
      data-continue={String(props.continueOnFailure)}
      data-block-type={props.blockType}
      data-hide-top={String(props.hideTopSeparator ?? false)}
    >
      <button
        data-testid="continue-on-failure-toggle"
        onClick={() =>
          props.onContinueOnFailureChange(!props.continueOnFailure)
        }
      />
    </div>
  ),
}));

vi.mock("@/components/ui/switch", () => ({
  Switch: (props: {
    "data-testid"?: string;
    checked: boolean;
    onCheckedChange: (checked: boolean) => void;
    disabled?: boolean;
  }) => (
    <button
      role="switch"
      aria-checked={props.checked}
      data-testid={props["data-testid"] ?? "wait-for-completion-switch"}
      disabled={props.disabled}
      onClick={() => props.onCheckedChange(!props.checked)}
    />
  ),
}));

vi.mock("@/components/ui/accordion", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Accordion: Pass,
    AccordionItem: Pass,
    AccordionTrigger: ({ children }: { children?: ReactNode }) => (
      <button data-testid="accordion-trigger">{children}</button>
    ),
    AccordionContent: Pass,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { WorkflowTriggerBlockForm } from "./WorkflowTriggerBlockForm";

beforeEach(() => {
  vi.useFakeTimers();
  mockNodes.clear();
  updateNodeData.mockReset();
  beginInternalUpdate.mockReset();
  endInternalUpdate.mockReset();
  useTargetWorkflowParametersQueryMock.mockReset();
  useTargetWorkflowParametersQueryMock.mockReturnValue({
    workflowParameters: [],
    isLoading: false,
    isError: false,
    workflowTitle: "",
  });
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

function setWorkflowTriggerNode(
  id: string,
  overrides: Partial<WorkflowTriggerNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "workflowTrigger",
    data: {
      debuggable: true,
      label: "workflow_trigger_1",
      editable: true,
      model: null,
      continueOnFailure: false,
      workflowPermanentId: "",
      workflowTitle: "",
      payload: "{}",
      waitForCompletion: true,
      browserSessionId: "",
      useParentBrowserSession: true,
      parameterKeys: [],
      ...overrides,
    },
  });
}

describe("WorkflowTriggerBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(
      <WorkflowTriggerBlockForm blockId="missing" />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("w1", { id: "w1", type: "task", data: {} });
    const { container } = render(<WorkflowTriggerBlockForm blockId="w1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders WorkflowSelector + payload placeholder when no workflow selected", () => {
    setWorkflowTriggerNode("w1", { workflowPermanentId: "" });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    expect(screen.getByText("Target Workflow")).toBeDefined();
    expect(screen.getByTestId("workflow-selector")).toBeDefined();
    expect(
      screen.getByText(
        "Select a target workflow to configure its input parameters here.",
      ),
    ).toBeDefined();
    expect(screen.queryByTestId("payload-parameter-fields")).toBeNull();
  });

  test("renders WorkflowSelector + PayloadParameterFields when workflow selected", () => {
    setWorkflowTriggerNode("w1", {
      workflowPermanentId: "wpid_target",
      payload: '{"foo":"bar"}',
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    expect(screen.getByTestId("payload-parameter-fields")).toBeDefined();
    expect(
      screen.queryByText(
        "Select a target workflow to configure its input parameters here.",
      ),
    ).toBeNull();
    const payloadValue = screen.getByTestId("payload-value");
    expect(payloadValue.textContent).toBe('{"foo":"bar"}');
  });

  test("excludeWorkflowPermanentId is sourced from useParams()", () => {
    setWorkflowTriggerNode("w1");
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    const exclude = screen.getByTestId("workflow-selector-exclude");
    expect(exclude.textContent).toBe("wpid_parent");
  });

  test("selecting a workflow propagates workflowPermanentId AND resets payload to {}", () => {
    setWorkflowTriggerNode("w1", {
      workflowPermanentId: "wpid_initial",
      payload: '{"keep":"me"}',
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("workflow-selector-change"));
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      workflowPermanentId: "wpid_other",
      payload: "{}",
    });
  });

  test("selecting the same workflow does not propagate", () => {
    setWorkflowTriggerNode("w1", {
      workflowPermanentId: "wpid_initial",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("workflow-selector-change-same"));
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("editing payload propagates", () => {
    setWorkflowTriggerNode("w1", {
      workflowPermanentId: "wpid_target",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("payload-change"));
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      payload: '{"foo":"bar"}',
    });
  });

  test("toggling 'Use dynamic value' reveals WorkflowBlockInputTextarea", () => {
    setWorkflowTriggerNode("w1", {
      useParentBrowserSession: true,
      browserSessionId: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    expect(screen.getByTestId("browser-session-selector")).toBeDefined();
    expect(
      screen.queryByTestId("wbi-ph-e.g. {{ browser_session_id }}"),
    ).toBeNull();

    fireEvent.click(screen.getByText("Use dynamic value"));

    expect(
      screen.getByTestId("wbi-ph-e.g. {{ browser_session_id }}"),
    ).toBeDefined();
    expect(screen.queryByTestId("browser-session-selector")).toBeNull();
  });

  test("editing browserSessionId in dynamic mode propagates with useParentBrowserSession=false", () => {
    setWorkflowTriggerNode("w1", {
      useParentBrowserSession: false,
      browserSessionId: "session_existing",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    const input = screen.getByTestId(
      "wbi-ph-e.g. {{ browser_session_id }}",
    ) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "session_new" } });

    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      useParentBrowserSession: false,
      browserSessionId: "session_new",
    });
  });

  test("selecting PARENT_SESSION_VALUE in BrowserSessionSelector sets useParentBrowserSession=true and browserSessionId=''", () => {
    setWorkflowTriggerNode("w1", {
      useParentBrowserSession: false,
      browserSessionId: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("browser-session-parent"));
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      useParentBrowserSession: true,
      browserSessionId: "",
    });
  });

  test("selecting FRESH_SESSION_VALUE clears parent and id", () => {
    setWorkflowTriggerNode("w1", {
      useParentBrowserSession: true,
      browserSessionId: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("browser-session-fresh"));
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      useParentBrowserSession: false,
      browserSessionId: "",
    });
  });

  test("toggling waitForCompletion off when useParentBrowserSession is true also resets browser session", () => {
    setWorkflowTriggerNode("w1", {
      waitForCompletion: true,
      useParentBrowserSession: true,
      browserSessionId: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    fireEvent.click(screen.getByTestId("wait-for-completion-switch"));
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      waitForCompletion: false,
      useParentBrowserSession: false,
      browserSessionId: "",
    });
  });

  test("shows warning text when !waitForCompletion && !useDynamicBrowserSession", () => {
    setWorkflowTriggerNode("w1", {
      waitForCompletion: false,
      useParentBrowserSession: true,
      browserSessionId: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    expect(
      screen.getByText(
        /Continue in the same session.*disabled because the parent workflow may close its browser/i,
      ),
    ).toBeDefined();
  });

  test("registers/unregisters commit", () => {
    setWorkflowTriggerNode("w1");
    const { unmount } = render(<WorkflowTriggerBlockForm blockId="w1" />);
    expect(usePendingCommitsStore.getState().commits["w1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["w1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setWorkflowTriggerNode("w1");
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("w1");
    });
    expect(ok).toBe(true);
  });

  test("workflowTitle hydration writes fetched title and brackets internal-update flag", () => {
    useTargetWorkflowParametersQueryMock.mockReturnValue({
      workflowParameters: [],
      isLoading: false,
      isError: false,
      workflowTitle: "Fetched Title",
    });
    setWorkflowTriggerNode("w1", {
      workflowPermanentId: "wpid_target",
      workflowTitle: "",
    });
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    expect(beginInternalUpdate).toHaveBeenCalled();
    expect(updateNodeData).toHaveBeenCalledWith("w1", {
      workflowTitle: "Fetched Title",
    });

    act(() => {
      vi.advanceTimersByTime(60);
    });
    expect(endInternalUpdate).toHaveBeenCalled();
  });

  test("passes hideTopSeparator to BlockExecutionOptions", () => {
    setWorkflowTriggerNode("w1");
    render(<WorkflowTriggerBlockForm blockId="w1" />);

    const beo = screen.getByTestId("block-execution-options");
    expect(beo.getAttribute("data-hide-top")).toBe("true");
    expect(beo.getAttribute("data-block-type")).toBe("workflowTrigger");
  });
});
