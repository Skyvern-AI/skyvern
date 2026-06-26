// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import { useWorkflowPanelStore } from "./WorkflowPanelStore";

beforeEach(() => {
  useWorkflowPanelStore.setState({
    workflowPanelState: { active: false, content: "parameters" },
    selectedBlockId: null,
  });
});

describe("WorkflowPanelStore", () => {
  test("setSelectedBlockId replaces any prior selection", () => {
    const store = useWorkflowPanelStore.getState();
    store.setSelectedBlockId("block-a");
    store.setSelectedBlockId("block-b");
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBe("block-b");
  });

  test("closeWorkflowPanel deactivates the panel but preserves selectedBlockId", () => {
    const store = useWorkflowPanelStore.getState();
    store.setWorkflowPanelState({ active: true, content: "nodeLibrary" });
    store.setSelectedBlockId("block-1");

    useWorkflowPanelStore.getState().closeWorkflowPanel();

    const next = useWorkflowPanelStore.getState();
    expect(next.workflowPanelState.active).toBe(false);
    expect(next.selectedBlockId).toBe("block-1");
  });
});
