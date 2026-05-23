// @vitest-environment jsdom

import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";

import { NodeGripHandle } from "../nodes/components/NodeGripHandle";
import { POINTER_ACTIVATION_DISTANCE_PX } from "../sortable/dragSensors";
import { withSortableBlock } from "../sortable/withSortableBlock";
import { useNodeCollapseStore } from "./useNodeCollapseStore";
import { withCollapsible } from "./withCollapsible";

beforeAll(() => {
  // dnd-kit's PointerSensor calls `setPointerCapture` on activation.
  // jsdom doesn't implement the Pointer Events capture API.
  if (!("setPointerCapture" in HTMLElement.prototype)) {
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      value: function () {},
      configurable: true,
    });
  }
  if (!("releasePointerCapture" in HTMLElement.prototype)) {
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      value: function () {},
      configurable: true,
    });
  }
  if (!("hasPointerCapture" in HTMLElement.prototype)) {
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
      value: function () {
        return false;
      },
      configurable: true,
    });
  }
});

beforeEach(() => {
  useNodeCollapseStore.setState({ collapsed: {} });
});

afterEach(() => {
  cleanup();
});

const FIRST_NODE_ID = "block-alpha";
const SECOND_NODE_ID = "block-beta";

type FakeAppNodeProps = NodeProps & { blockLabel?: string };

function FakeAppNode(props: FakeAppNodeProps) {
  const { blockLabel } = props;
  return (
    <div>
      <span data-testid="expanded-body">Fake block {blockLabel ?? ""}</span>
      <NodeGripHandle blockLabel={blockLabel} />
    </div>
  );
}

// Composition order matches nodes/index.ts: withSortableBlock outside
// withCollapsible so useSortable fires regardless of collapse state.
const Wrapped = withSortableBlock(
  withCollapsible(FakeAppNode as never),
) as React.FC<
  Partial<FakeAppNodeProps> & {
    id: string;
    blockLabel?: string;
    data?: unknown;
  }
>;

function DndHarness({
  onDragEnd,
}: {
  onDragEnd: (event: DragEndEvent) => void;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE_PX },
    }),
  );
  const items = [FIRST_NODE_ID, SECOND_NODE_ID];
  return (
    <DndContext sensors={sensors} onDragEnd={onDragEnd}>
      <SortableContext items={items} strategy={verticalListSortingStrategy}>
        <Wrapped
          id={FIRST_NODE_ID}
          blockLabel="Alpha"
          data={{ label: "Alpha" } as never}
        />
        <Wrapped
          id={SECOND_NODE_ID}
          blockLabel="Beta"
          data={{ label: "Beta" } as never}
        />
      </SortableContext>
    </DndContext>
  );
}

function alphaGrip(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: "Drag to reorder block Alpha",
  }) as HTMLButtonElement;
}

describe("withSortableBlock + withCollapsible drag integration (SKY-9069 / SKY-9361)", () => {
  test("inner component renders in both expanded and collapsed states", () => {
    render(<DndHarness onDragEnd={() => {}} />);
    expect(screen.getAllByTestId("expanded-body")).toHaveLength(2);
  });

  test("collapsed state still renders the inner component (HOC no longer swaps)", () => {
    useNodeCollapseStore.setState({
      collapsed: { "__global__\x1fAlpha": true },
    });
    render(<DndHarness onDragEnd={() => {}} />);
    // Both blocks still render — Radix Collapsible only animates the body
    // wrapper. The grip + body sibling stays mounted in this fake node.
    expect(screen.getAllByTestId("expanded-body")).toHaveLength(2);
  });

  test("a 10 px drag on a collapsed block's grip still fires onDragEnd", () => {
    // The core SKY-9069 invariant: collapsing a block is a UI tweak, not
    // a drag gate. The HOC composition order (withSortableBlock OUTSIDE
    // withCollapsible) means `useSortable({ id })` fires regardless of
    // collapse state, so dnd-kit can still pick up the grip's pointerdown
    // and drive the reorder.
    useNodeCollapseStore.setState({
      collapsed: { "__global__\x1fAlpha": true },
    });
    const onDragEnd = vi.fn();
    render(<DndHarness onDragEnd={onDragEnd} />);

    const grip = alphaGrip();
    fireEvent.pointerDown(grip, {
      pointerId: 1,
      button: 0,
      isPrimary: true,
      clientX: 0,
      clientY: 0,
    });
    fireEvent.pointerMove(grip.ownerDocument, {
      pointerId: 1,
      isPrimary: true,
      clientX: 10,
      clientY: 0,
    });
    fireEvent.pointerUp(grip.ownerDocument, {
      pointerId: 1,
      isPrimary: true,
      clientX: 10,
      clientY: 0,
    });

    expect(onDragEnd).toHaveBeenCalledTimes(1);
    const firstCall = onDragEnd.mock.calls[0];
    if (!firstCall) throw new Error("onDragEnd not invoked");
    const event = firstCall[0] as DragEndEvent;
    expect(String(event.active.id)).toBe(FIRST_NODE_ID);
  });
});
