// @vitest-environment jsdom

import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
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
} from "vitest";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { NodeGripHandle } from "../nodes/components/NodeGripHandle";
import { POINTER_ACTIVATION_DISTANCE_PX } from "../sortable/dragSensors";
import { withSortableBlock } from "../sortable/withSortableBlock";
import { withSelectableBlock } from "./withSelectableBlock";

/**
 * SKY-9360 — drag-vs-click selection invariant.
 *
 * Pins the architectural separation between dnd-kit drag activation and
 * React Flow's click-to-select wiring:
 *
 *   1. The grip handle's `onPointerDown` calls `event.stopPropagation()`
 *      and forwards the gesture to dnd-kit's PointerSensor — so a drag
 *      that starts on the grip never reaches the click path that would
 *      flip `selectedBlockId`.
 *   2. A click on the node body (anywhere outside the grip handle) is
 *      what FlowRenderer's `onNodeClick` captures, and it routes through
 *      `setSelectedBlockId(node.id)`.
 *
 * Tests render the full HOC pipeline `withSortableBlock(withSelectableBlock(...))`
 * inside a `DndContext` + `SortableContext` so the assertion fails if any
 * link in that chain regresses (HOC, grip wiring, sensor threshold, store
 * subscription). The body's click handler in the harness mirrors what
 * FlowRenderer.tsx wires into React Flow's `onNodeClick`.
 */

beforeAll(() => {
  // dnd-kit's PointerSensor calls `setPointerCapture` on the target when a
  // drag activates. jsdom does not implement the Pointer Events capture API
  // by default, so patch it to a no-op (mirrors wireSortable.test.tsx).
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
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
});

afterEach(() => {
  cleanup();
});

const NODE_ID = "block-alpha";

type FakeAppNodeProps = NodeProps & { blockLabel?: string };

function FakeAppNode({ id, blockLabel }: FakeAppNodeProps) {
  // Mirrors the production wiring: a click on the node body routes through
  // FlowRenderer's `onNodeClick` which calls `setSelectedBlockId(node.id)`.
  // The harness binds the equivalent listener on the body so the test can
  // distinguish "click reached the body" from "drag short-circuited it".
  const setSelectedBlockId = useWorkflowPanelStore(
    (state) => state.setSelectedBlockId,
  );
  return (
    <div
      data-testid={`node-root-${id}`}
      onClick={() => setSelectedBlockId(String(id))}
    >
      <NodeGripHandle blockLabel={blockLabel} />
      <span data-testid={`node-body-${id}`}>Fake block body {blockLabel}</span>
    </div>
  );
}

const FakeWrappedNode = withSortableBlock(
  withSelectableBlock(FakeAppNode as never),
) as React.FC<{ id: string; blockLabel?: string }>;

function DndHarness() {
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE_PX },
    }),
  );
  return (
    <DndContext sensors={sensors}>
      <SortableContext items={[NODE_ID]} strategy={verticalListSortingStrategy}>
        <FakeWrappedNode id={NODE_ID} blockLabel="Alpha" />
      </SortableContext>
    </DndContext>
  );
}

function gripHandle(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: "Drag to reorder block Alpha",
  }) as HTMLButtonElement;
}

describe("drag-vs-click selection invariant (SKY-9360)", () => {
  test("a click on the node body sets selectedBlockId to the node id", () => {
    render(<DndHarness />);
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();

    fireEvent.click(screen.getByTestId(`node-body-${NODE_ID}`));

    expect(useWorkflowPanelStore.getState().selectedBlockId).toBe(NODE_ID);
  });

  test("dragging the grip handle does NOT change selectedBlockId", () => {
    render(<DndHarness />);
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();

    const grip = gripHandle();
    // Full pointer drag sequence past the 5px activation threshold. Mirrors
    // the dispatch pattern in wireSortable.test.tsx (pointerdown on grip,
    // move + up on ownerDocument because the sensor swaps to document-level
    // listeners after activation).
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
      clientX: 12,
      clientY: 0,
    });
    fireEvent.pointerUp(grip.ownerDocument, {
      pointerId: 1,
      isPrimary: true,
      clientX: 12,
      clientY: 0,
    });

    expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();
  });

  test("a 0px click on the grip handle does NOT change selectedBlockId", () => {
    // Belt-and-suspenders: even when the user just presses + releases on
    // the grip without dragging, the grip's `event.stopPropagation()` keeps
    // the gesture from bubbling into the body's click path.
    render(<DndHarness />);
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();

    const grip = gripHandle();
    fireEvent.pointerDown(grip, {
      pointerId: 1,
      button: 0,
      isPrimary: true,
      clientX: 0,
      clientY: 0,
    });
    fireEvent.pointerUp(grip, {
      pointerId: 1,
      isPrimary: true,
      clientX: 0,
      clientY: 0,
    });

    expect(useWorkflowPanelStore.getState().selectedBlockId).toBeNull();
  });
});
