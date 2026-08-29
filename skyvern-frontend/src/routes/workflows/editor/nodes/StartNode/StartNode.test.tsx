// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test, vi } from "vitest";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { StartNode } from "./StartNode";
import {
  OPEN_WORKFLOW_SETTINGS_EVENT,
  type WorkflowStartNodeData,
} from "./types";

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    Handle: () => null,
    useReactFlow: () => ({
      getNode: () => null,
      getNodes: () => [],
    }),
  };
});

vi.mock("./WorkflowSettingsEditor", () => ({
  WorkflowSettingsEditor: () => <div data-testid="workflow-settings-editor" />,
}));

vi.mock("@/routes/workflows/hooks/useToggleScriptForNodeCallback", () => ({
  useToggleScriptForNodeCallback: () => vi.fn(),
}));

vi.mock("@/routes/workflows/components/BlockCodeEditor", () => ({
  BlockCodeEditor: () => null,
}));

const startNodeData: WorkflowStartNodeData = {
  withWorkflowSettings: true,
  webhookCallbackUrl: "",
  proxyLocation: null,
  persistBrowserSession: false,
  reuseBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  editable: true,
  runWith: "agent",
  codeVersion: null,
  scriptCacheKey: null,
  aiFallback: true,
  enableSelfHealing: false,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  label: "__start_block__",
  showCode: false,
};

type StartNodeComponentProps = {
  id: string;
  data: WorkflowStartNodeData;
  parentId?: string;
};
const StartNodeForTest = StartNode as unknown as (
  props: StartNodeComponentProps,
) => JSX.Element;

function renderStartNode(overrides: Partial<WorkflowStartNodeData> = {}) {
  return render(
    <MemoryRouter initialEntries={["/workflows/wpid_abc/studio"]}>
      <StartNodeForTest id="start" data={{ ...startNodeData, ...overrides }} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  useWorkflowParametersStore.setState({ parameters: [] });
  useWorkflowPanelStore.setState({
    workflowPanelState: { active: false, content: "parameters" },
  });
});

describe("StartNode inputs summary", () => {
  test("states the declared inputs, and Add opens the Inputs panel", () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "order_id",
          parameterType: "workflow",
          dataType: "string",
          defaultValue: null,
        },
        {
          key: "vendor_email",
          parameterType: "workflow",
          dataType: "string",
          defaultValue: null,
        },
      ],
    });
    renderStartNode();

    expect(screen.getByText("order_id")).toBeDefined();
    expect(screen.getByText("vendor_email")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /add/i }));

    expect(useWorkflowPanelStore.getState().workflowPanelState).toEqual({
      active: true,
      content: "parameters",
    });
  });

  test("an agent with no inputs says what inputs are, rather than nothing", () => {
    // The zero-input case is the one the header never distinguished, and the
    // reason nobody found the feature (SKY-14866).
    renderStartNode();

    expect(screen.getByText(/None yet/)).toBeDefined();
  });

  test("a view-only workflow can read its inputs but not add one", () => {
    // editable is false for global workflows and deleted snapshots, which the
    // editor headers already hide Inputs for. WorkflowParametersPanel mutates,
    // so reaching it here would dirty a workflow that cannot be saved.
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "order_id",
          parameterType: "workflow",
          dataType: "string",
          defaultValue: null,
        },
      ],
    });
    renderStartNode({ editable: false });

    expect(screen.getByText("order_id")).toBeDefined();
    expect(screen.queryByRole("button", { name: /add/i })).toBeNull();
  });

  test("Add does not also open workflow settings", () => {
    // The click bubbles to FlowRenderer's onNodeClick, which dispatches
    // OPEN_WORKFLOW_SETTINGS_EVENT for the root start node.
    const onNodeClick = vi.fn();
    render(
      <MemoryRouter initialEntries={["/workflows/wpid_abc/studio"]}>
        <div onClick={onNodeClick}>
          <StartNodeForTest id="start" data={startNodeData} />
        </div>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: /add/i }));

    expect(onNodeClick).not.toHaveBeenCalled();
    expect(useWorkflowPanelStore.getState().workflowPanelState).toEqual({
      active: true,
      content: "parameters",
    });
  });
});

describe("StartNode workflow settings affordance", () => {
  test("renders the Workflow Settings entry collapsed by default", () => {
    renderStartNode();

    expect(screen.getByText("Workflow Settings")).toBeDefined();
    expect(screen.queryByTestId("workflow-settings-editor")).toBeNull();
  });

  test("a canvas click event expands the settings accordion", () => {
    renderStartNode();

    act(() => {
      window.dispatchEvent(new Event(OPEN_WORKFLOW_SETTINGS_EVENT));
    });

    expect(screen.getByTestId("workflow-settings-editor")).toBeDefined();
  });

  test("the accordion trigger still toggles the settings manually", () => {
    renderStartNode();

    const trigger = screen.getByText("Workflow Settings");
    fireEvent.click(trigger);
    expect(screen.getByTestId("workflow-settings-editor")).toBeDefined();

    fireEvent.click(trigger);
    expect(screen.queryByTestId("workflow-settings-editor")).toBeNull();
  });

  test("the open event keeps already-open settings mounted", () => {
    renderStartNode();

    fireEvent.click(screen.getByText("Workflow Settings"));
    expect(screen.getByTestId("workflow-settings-editor")).toBeDefined();

    act(() => {
      window.dispatchEvent(new Event(OPEN_WORKFLOW_SETTINGS_EVENT));
    });

    expect(screen.getByTestId("workflow-settings-editor")).toBeDefined();
  });

  test("a trigger click that closes is not undone by the bubbled canvas dispatch", () => {
    // The canvas onNodeClick dispatches the open event from the same native
    // click that toggled the trigger, before React commits the close; the
    // listener must read the still-committed "open" value and stay quiet.
    renderStartNode();
    const trigger = screen.getByText("Workflow Settings");
    fireEvent.click(trigger);
    expect(screen.getByTestId("workflow-settings-editor")).toBeDefined();

    const bubbleDispatch = () =>
      window.dispatchEvent(new Event(OPEN_WORKFLOW_SETTINGS_EVENT));
    window.addEventListener("click", bubbleDispatch);
    try {
      fireEvent.click(trigger);
    } finally {
      window.removeEventListener("click", bubbleDispatch);
    }

    expect(screen.queryByTestId("workflow-settings-editor")).toBeNull();
  });
});
