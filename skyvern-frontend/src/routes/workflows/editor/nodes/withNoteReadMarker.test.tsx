import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { WorkflowNoteReadersProvider } from "../WorkflowNoteReadersProvider";
import { withNoteReadMarker } from "./withNoteReadMarker";

function BareNode({ id }: NodeProps) {
  return <div>{id}</div>;
}

const MarkerNode = withNoteReadMarker(BareNode);

function nodeProps(id: string): NodeProps {
  return {
    id,
    data: {},
    type: "task",
    dragging: false,
    zIndex: 0,
    selectable: true,
    deletable: true,
    selected: false,
    draggable: true,
    isConnectable: true,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
  };
}

describe("withNoteReadMarker", () => {
  test("renders one marker for each of six single-note steps and none for an unnoted step", async () => {
    const notes = new Map(
      Array.from({ length: 6 }, (_, index) => [
        `step-${index + 1}`,
        [`Note ${index + 1}`],
      ]),
    );

    render(
      <WorkflowNoteReadersProvider readers={notes}>
        {Array.from({ length: 7 }, (_, index) => (
          <MarkerNode key={index} {...nodeProps(`step-${index + 1}`)} />
        ))}
      </WorkflowNoteReadersProvider>,
    );

    const markers = screen.getAllByRole("button", {
      name: "This step has 1 note",
    });
    expect(markers).toHaveLength(6);
    fireEvent.pointerMove(markers[0]!);
    expect(await screen.findAllByText("Note 1")).not.toHaveLength(0);
    expect(screen.queryByText("step-7")).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: "This step has 0 notes" }),
    ).toBeNull();
  });

  test("keeps the tooltip within a 390px viewport", async () => {
    // jsdom has no layout; supply measured sizes and exercise Radix's real collision placement.
    const isTooltip = (element: HTMLElement) =>
      element.hasAttribute("data-radix-popper-content-wrapper") ||
      element.hasAttribute("data-side");
    const spies = [
      vi
        .spyOn(document.documentElement, "clientWidth", "get")
        .mockReturnValue(390),
      vi
        .spyOn(document.documentElement, "clientHeight", "get")
        .mockReturnValue(844),
      vi
        .spyOn(HTMLElement.prototype, "offsetWidth", "get")
        .mockImplementation(function (this: HTMLElement) {
          return isTooltip(this)
            ? 374
            : this === document.documentElement
              ? 390
              : 20;
        }),
      vi
        .spyOn(HTMLElement.prototype, "offsetHeight", "get")
        .mockImplementation(function (this: HTMLElement) {
          return isTooltip(this)
            ? 120
            : this === document.documentElement
              ? 844
              : 20;
        }),
      vi
        .spyOn(HTMLElement.prototype, "getBoundingClientRect")
        .mockImplementation(function (this: HTMLElement) {
          if (this.tagName === "BUTTON") return new DOMRect(300, 200, 20, 20);
          if (isTooltip(this)) return new DOMRect(0, 0, 374, 120);
          return new DOMRect(0, 0, 390, 844);
        }),
    ];
    try {
      render(
        <WorkflowNoteReadersProvider
          readers={new Map([["step", ["A long note".repeat(30)]]])}
        >
          <MarkerNode {...nodeProps("step")} />
        </WorkflowNoteReadersProvider>,
      );
      fireEvent.focus(
        screen.getByRole("button", { name: "This step has 1 note" }),
      );
      await screen.findByRole("tooltip");
      await waitFor(() => {
        const wrapper = document.querySelector<HTMLElement>(
          "[data-radix-popper-content-wrapper]",
        )!;
        const translate = wrapper.style.transform.match(
          /translate\(([-\d.]+)px, ([-\d.]+)px\)/,
        );
        expect(translate).not.toBeNull();
        const left = Number(translate![1]);
        expect(left).toBeGreaterThanOrEqual(8);
        expect(left + wrapper.offsetWidth).toBeLessThanOrEqual(382);
      });
    } finally {
      spies.forEach((spy) => spy.mockRestore());
    }
  });

  test("a step keeps its state when its first note appears", () => {
    function StatefulNode({ id }: NodeProps) {
      const [open, setOpen] = useState(false);
      return (
        <button type="button" onClick={() => setOpen(true)}>
          {id} {open ? "open" : "closed"}
        </button>
      );
    }
    const Marked = withNoteReadMarker(StatefulNode);
    const view = (readers: Map<string, string[]>) => (
      <WorkflowNoteReadersProvider readers={readers}>
        <Marked {...nodeProps("step")} />
      </WorkflowNoteReadersProvider>
    );
    const { rerender } = render(view(new Map()));
    fireEvent.click(screen.getByRole("button", { name: "step closed" }));

    rerender(view(new Map([["step", ["First note"]]])));

    expect(screen.getByRole("button", { name: "step open" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "This step has 1 note" }),
    ).toBeTruthy();
  });
});
