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
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
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

function renderStartNode() {
  return render(
    <MemoryRouter initialEntries={["/workflows/wpid_abc/studio"]}>
      <StartNodeForTest id="start" data={startNodeData} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
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
