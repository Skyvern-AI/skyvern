// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { CollapseContext } from "./CollapseContext";
import { NodeBody } from "./NodeBody";
import { useNodeCollapseStore } from "./useNodeCollapseStore";
import { withCollapsible } from "./withCollapsible";

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

function Inner(props: NodeProps) {
  const data = (props.data as { label: string }) ?? { label: "" };
  return (
    <div data-testid="inner">
      <span data-testid="header">{data.label}</span>
      <NodeBody>
        <span data-testid="body">body</span>
      </NodeBody>
    </div>
  );
}

const Wrapped = withCollapsible(Inner);

beforeEach(() => {
  useNodeCollapseStore.setState({ collapsed: {} });
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("withCollapsible HOC (SKY-9069 / SKY-9361)", () => {
  test("renders the inner component with body open by default", () => {
    render(<Wrapped {...makeProps()} />);
    expect(screen.getByTestId("inner")).toBeDefined();
    expect(screen.getByTestId("header").textContent).toBe("Alpha");
    const wrapper = screen.getByTestId("body").closest("[data-state]")!;
    expect(wrapper.getAttribute("data-state")).toBe("open");
  });

  test("renders the inner component with body unmounted when collapsed", () => {
    useNodeCollapseStore.setState({
      collapsed: { "__global__\x1fAlpha": true },
    });
    render(<Wrapped {...makeProps()} />);
    expect(screen.getByTestId("inner")).toBeDefined();
    expect(screen.queryByTestId("body")).toBeNull();
  });

  test("re-renders live when the store transitions for this label", () => {
    render(<Wrapped {...makeProps()} />);
    expect(screen.getByTestId("body")).toBeDefined();

    act(() => {
      useNodeCollapseStore.getState().toggleBlock("__global__", "Alpha");
    });
    expect(screen.queryByTestId("body")).toBeNull();

    act(() => {
      useNodeCollapseStore.getState().toggleBlock("__global__", "Alpha");
    });
    expect(screen.getByTestId("body")).toBeDefined();
  });

  test("label-keyed isolation - collapsing Alpha does not collapse Beta", () => {
    useNodeCollapseStore.setState({
      collapsed: { "__global__\x1fAlpha": true },
    });
    render(<Wrapped {...makeProps({ data: { label: "Beta" } })} />);
    expect(screen.getByTestId("body")).toBeDefined();
  });

  test("provides CollapseContext value to descendants", () => {
    function Probe() {
      return (
        <CollapseContext.Consumer>
          {({ open }) => (
            <div data-testid="ctx">{open ? "open" : "closed"}</div>
          )}
        </CollapseContext.Consumer>
      );
    }
    function HostInner() {
      return (
        <div>
          <Probe />
          <NodeBody>
            <span data-testid="body">body</span>
          </NodeBody>
        </div>
      );
    }
    const Host = withCollapsible(HostInner as unknown as typeof Inner);
    render(<Host {...makeProps()} />);
    expect(screen.getByTestId("ctx").textContent).toBe("open");
  });

  test("missing label falls back to expanded state", () => {
    render(<Wrapped {...makeProps({ data: {} })} />);
    expect(screen.getByTestId("body")).toBeDefined();
  });

  test("displayName wraps the inner component's name", () => {
    expect(Wrapped.displayName).toBe("withCollapsible(Inner)");
  });
});
