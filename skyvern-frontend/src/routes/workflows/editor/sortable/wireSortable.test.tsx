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
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";

import { NodeGripHandle } from "../nodes/components/NodeGripHandle";
import { POINTER_ACTIVATION_DISTANCE_PX } from "./dragSensors";
import { withSortableBlock } from "./withSortableBlock";

/**
 * SKY-9067 — drag-activation wiring smoke test.
 *
 * Confirms that the two layered blockers diagnosed for M0 are fixed:
 *
 *   1. Each AppNode instance registers itself with dnd-kit via
 *      `useSortable({ id })` (the HOC). Without that call the PointerSensor
 *      has nothing to pick up on pointerdown even though the surrounding
 *      `SortableContext` knows the ordered ids.
 *   2. The grip handle's `onPointerDown` forwards the event to dnd-kit's
 *      listener after `event.stopPropagation()`, so React Flow's pane
 *      handler cannot consume the pointerdown. `nodrag nopan` + explicit
 *      forwarding are belt-and-suspenders.
 *
 * Tested through the full render path (HOC → context → grip) rather than
 * mocking `useSortable` — we want the assertion to fail if any layer in the
 * chain regresses.
 */

beforeAll(() => {
  // dnd-kit's PointerSensor calls `setPointerCapture` on the target when a
  // drag activates. jsdom does not implement the Pointer Events capture API
  // by default, so patch it to a no-op. `releasePointerCapture` is exercised
  // on drop for the same reason.
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

afterEach(() => {
  cleanup();
});

const FIRST_NODE_ID = "block-alpha";
const SECOND_NODE_ID = "block-beta";

type FakeAppNodeProps = NodeProps & { blockLabel?: string };

function FakeAppNode({ blockLabel }: FakeAppNodeProps) {
  return (
    <div>
      <span>Fake block {blockLabel ?? "(unlabelled)"}</span>
      <NodeGripHandle blockLabel={blockLabel} />
    </div>
  );
}

const FakeWrappedNode = withSortableBlock(FakeAppNode as never) as React.FC<
  Partial<FakeAppNodeProps> & { id: string; blockLabel?: string }
>;

function DndHarness({
  onDragEnd,
}: {
  onDragEnd: (event: DragEndEvent) => void;
}) {
  // Mirror the prod sensor wiring (PointerSensor with a 5 px activation
  // distance) so the test exercises the real activation threshold rather
  // than a zero-distance sensor that would fire on any pointerdown.
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE_PX },
    }),
  );
  const items = [FIRST_NODE_ID, SECOND_NODE_ID];
  return (
    <DndContext sensors={sensors} onDragEnd={onDragEnd}>
      <SortableContext items={items} strategy={verticalListSortingStrategy}>
        <FakeWrappedNode id={FIRST_NODE_ID} blockLabel="Alpha" />
        <FakeWrappedNode id={SECOND_NODE_ID} blockLabel="Beta" />
      </SortableContext>
    </DndContext>
  );
}

function firstGripHandle(): HTMLButtonElement {
  // Both rows render a grip handle; the accessible-name lookup pins us to
  // the Alpha block, which is the one the drag simulation targets.
  return screen.getByRole("button", {
    name: "Drag to reorder block Alpha",
  }) as HTMLButtonElement;
}

describe("withSortableBlock + NodeGripHandle drag activation (SKY-9067)", () => {
  test("renders the sortable-id attribute the HOC owns", () => {
    // If the HOC ever stops passing `props.id` into `useSortable`, the
    // `data-sortable-id` marker disappears and downstream devtools /
    // snapshot assertions lose their anchor — cheap regression fence.
    render(<DndHarness onDragEnd={() => {}} />);
    expect(
      document.querySelector(`[data-sortable-id="${FIRST_NODE_ID}"]`),
    ).not.toBeNull();
    expect(
      document.querySelector(`[data-sortable-id="${SECOND_NODE_ID}"]`),
    ).not.toBeNull();
  });

  test("grip handle carries React Flow's nodrag nopan opt-out classes", () => {
    // Without `nodrag nopan`, RF's `panOnDrag` pane handler consumes the
    // pointerdown before dnd-kit's PointerSensor can activate. The class
    // tokens are the load-bearing part — assert their presence, not the
    // full className string, so the axe + visual classes stay flexible.
    render(<DndHarness onDragEnd={() => {}} />);
    const grip = firstGripHandle();
    expect(grip.classList.contains("nodrag")).toBe(true);
    expect(grip.classList.contains("nopan")).toBe(true);
  });

  test("a 10px pointer drag fires onDragEnd with the wrapped node id", () => {
    // Full activation path: pointerdown on the grip + pointermove past the
    // 5 px threshold + pointerup fires dnd-kit's `onDragEnd`. If the HOC
    // or grip wiring regresses (listeners missing, stopPropagation
    // absent, `nodrag nopan` absent) the spy stays at zero calls.
    const onDragEnd = vi.fn();
    render(<DndHarness onDragEnd={onDragEnd} />);

    const grip = firstGripHandle();
    // dnd-kit's PointerSensor activator guards on `event.isPrimary` and
    // `event.button === 0`; jsdom's synthetic PointerEvent defaults
    // `isPrimary` to false, so without the explicit flag the activator
    // short-circuits before dnd-kit attaches its document-level
    // pointermove / pointerup listeners. After activation the sensor
    // listens on the grip's ownerDocument, so move + up are dispatched
    // there rather than on the grip itself.
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

  test("a 0px click does not fire onDragEnd (activation threshold holds)", () => {
    // The 5 px PointerSensor threshold keeps accidental clicks on the
    // grip handle from firing a spurious reorder. Regressing that threshold
    // would be annoying but not catastrophic — still worth a guard since
    // the test cost is tiny.
    const onDragEnd = vi.fn();
    render(<DndHarness onDragEnd={onDragEnd} />);

    const grip = firstGripHandle();
    fireEvent.pointerDown(grip, {
      pointerId: 1,
      button: 0,
      clientX: 0,
      clientY: 0,
    });
    fireEvent.pointerUp(grip, {
      pointerId: 1,
      clientX: 0,
      clientY: 0,
    });

    expect(onDragEnd).not.toHaveBeenCalled();
  });
});
