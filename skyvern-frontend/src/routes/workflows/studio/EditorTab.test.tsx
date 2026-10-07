// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunBlockingBlock } from "@/routes/workflows/editor/runValidation/getRunBlockingBlocks";
import { useLocateBlockStore } from "@/routes/workflows/editor/runValidation/useLocateBlockStore";
import { useRunBlockingPanelStore } from "@/routes/workflows/editor/runValidation/useRunBlockingPanelStore";
import { useRunValidationStore } from "@/routes/workflows/editor/runValidation/useRunValidationStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";

vi.mock("@/routes/workflows/editor/Workspace", () => ({
  Workspace: () => <div data-testid="workspace" />,
}));

const blockingBlock: RunBlockingBlock = {
  id: "nested-block",
  label: "missing_credential",
  ancestorIds: ["loop-1"],
  path: [{ kind: "loop", label: "loop_1" }],
};
// Workspace is mocked, so required canvas inputs are intentionally unused here.
const editorProps = {} as StudioWorkspaceProps;

describe("EditorTab run-blocking locator", () => {
  beforeEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([blockingBlock]);
    useLocateBlockStore.getState().clearLocate();
    useRunBlockingPanelStore.getState().setCollapsed(false);
    useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
  });

  afterEach(() => {
    cleanup();
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearLocate();
    useRunBlockingPanelStore.getState().setCollapsed(false);
    useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
  });

  it("renders the shared panel and routes Locate requests for nested blocks", () => {
    render(<EditorTab {...editorProps} />);

    expect(screen.getByTestId("workspace")).toBeTruthy();
    expect(screen.getByText("1 block needs fixing")).toBeTruthy();
    expect(screen.getByText("loop_1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Locate/ }));

    expect(useLocateBlockStore.getState().request?.nodeId).toBe("nested-block");
  });

  it("hides the locator while Studio shows a read-only comparison", () => {
    useWorkflowPanelStore.getState().setWorkflowPanelState({
      active: true,
      content: "comparison",
      data: { showComparison: true },
    });

    render(<EditorTab {...editorProps} />);

    expect(screen.queryByText("1 block needs fixing")).toBeNull();
  });
});
