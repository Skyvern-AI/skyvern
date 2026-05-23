// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { withSelectableBlock } from "./withSelectableBlock";

function makeProps(overrides: Partial<NodeProps> = {}): NodeProps {
  return {
    id: "node-alpha",
    type: "task",
    data: { label: "Alpha" },
    selected: false,
    dragging: false,
    zIndex: 0,
    isConnectable: true,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
    ...overrides,
  } as unknown as NodeProps;
}

function Wrapped(props: NodeProps) {
  return <div data-testid="wrapped">wrapped {String(props.id)}</div>;
}

const Selectable = withSelectableBlock(Wrapped);

beforeEach(() => {
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
});

afterEach(() => {
  cleanup();
});

describe("withSelectableBlock HOC (SKY-9358)", () => {
  test("renders the wrapped Component", () => {
    render(<Selectable {...makeProps()} />);
    expect(screen.getByTestId("wrapped")).toBeDefined();
  });

  test("does not mark the wrapper as selected by default", () => {
    render(<Selectable {...makeProps()} />);
    const wrapper = screen.getByTestId("wrapped").parentElement;
    expect(wrapper?.getAttribute("data-selected")).toBeNull();
  });

  test("marks the wrapper as selected when selectedBlockId matches", () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("node-alpha");
    render(<Selectable {...makeProps()} />);
    const wrapper = screen.getByTestId("wrapped").parentElement;
    expect(wrapper?.getAttribute("data-selected")).toBe("true");
  });

  test("ignores selection for a different node id", () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("node-beta");
    render(<Selectable {...makeProps()} />);
    const wrapper = screen.getByTestId("wrapped").parentElement;
    expect(wrapper?.getAttribute("data-selected")).toBeNull();
  });

  test("re-renders live when the store transitions for this id", () => {
    render(<Selectable {...makeProps()} />);
    const wrapper = screen.getByTestId("wrapped").parentElement;
    expect(wrapper?.getAttribute("data-selected")).toBeNull();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("node-alpha");
    });
    expect(wrapper?.getAttribute("data-selected")).toBe("true");

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId(null);
    });
    expect(wrapper?.getAttribute("data-selected")).toBeNull();
  });

  test("setting a new id auto-deselects the prior one (single-select)", () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("node-alpha");
    render(
      <>
        <Selectable {...makeProps()} />
        <Selectable {...makeProps({ id: "node-beta" })} />
      </>,
    );
    const wrappers = screen.getAllByTestId("wrapped");
    expect(wrappers[0]?.parentElement?.getAttribute("data-selected")).toBe(
      "true",
    );
    expect(
      wrappers[1]?.parentElement?.getAttribute("data-selected"),
    ).toBeNull();

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("node-beta");
    });

    expect(
      wrappers[0]?.parentElement?.getAttribute("data-selected"),
    ).toBeNull();
    expect(wrappers[1]?.parentElement?.getAttribute("data-selected")).toBe(
      "true",
    );
  });

  test("displayName wraps the inner component's name", () => {
    expect(Selectable.displayName).toBe("withSelectableBlock(Wrapped)");
  });
});
