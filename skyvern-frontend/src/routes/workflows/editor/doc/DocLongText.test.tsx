// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { DocLongText } from "./DocLongText";
import { DOC_COLLAPSED_LINES, docTextOverflows } from "./docTextFold";

const text = (lines: number) => {
  const el = document.createElement("span");
  el.style.lineHeight = "20px";
  Object.defineProperty(el, "scrollHeight", { value: lines * 20 });
  return el;
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("when a step's text folds", () => {
  test("a text of the collapsed height or half a line more stays whole; one more line folds", () => {
    expect(DOC_COLLAPSED_LINES).toBe(3);
    expect(docTextOverflows(text(DOC_COLLAPSED_LINES - 1))).toBe(false);
    expect(docTextOverflows(text(DOC_COLLAPSED_LINES))).toBe(false);
    expect(docTextOverflows(text(DOC_COLLAPSED_LINES + 0.5))).toBe(false);
    expect(docTextOverflows(text(DOC_COLLAPSED_LINES + 1))).toBe(true);
  });

  test("a text in the half-line slack is shown as it is; above it, it folds with a control; opened, it is not clipped", () => {
    let height = 3.4 * 24.8;
    Object.defineProperty(HTMLSpanElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => height,
    });
    function Host({ open }: { open: boolean }) {
      return (
        <DocLongText
          collapse={{ open, setOpen: () => {}, name: "Check" }}
          value={String(height)}
        >
          {(shown) => (
            <span ref={shown.ref} id={shown.id} style={shown.style}>
              x
            </span>
          )}
        </DocLongText>
      );
    }
    const shownText = () => document.querySelector<HTMLElement>("span[id]")!;
    try {
      const view = render(<Host open={false} />);
      expect(shownText().style.overflow).toBe("");
      expect(screen.queryByRole("button")).toBeNull();
      cleanup();

      height = 4 * 24.8;
      render(<Host open={false} />);
      expect(shownText().style.overflow).toBe("hidden");
      expect(
        screen.getByRole("button", { name: "Show more: Check" }),
      ).not.toBeNull();
      cleanup();

      render(<Host open />);
      expect(shownText().style.overflow).toBe("");
      void view;
    } finally {
      // @ts-expect-error removing the jsdom layout stub restores the prototype's own getter
      delete HTMLSpanElement.prototype.scrollHeight;
    }
  });

  test("Show less scrolls by the lines it hides, so the control stays where it was", () => {
    Object.defineProperty(HTMLSpanElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => 200,
    });
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <div data-testid="scroller" style={{ overflowY: "auto" }}>
          <DocLongText collapse={{ open, setOpen, name: "Check" }} value="x">
            {(shown) => (
              <span ref={shown.ref} id={shown.id} style={shown.style}>
                x
              </span>
            )}
          </DocLongText>
        </div>
      );
    }
    try {
      render(<Host />);
      const scroller = screen.getByTestId("scroller");
      let scrollTop = 1000;
      Object.defineProperty(scroller, "scrollTop", {
        get: () => scrollTop,
        set: (value: number) => {
          scrollTop = value;
        },
      });
      const less = screen.getByRole("button", { name: "Show less: Check" });
      // The control sits 300px higher once the text above it folds.
      vi.spyOn(less, "getBoundingClientRect").mockImplementation(
        () =>
          ({
            top:
              document.querySelector<HTMLElement>("span[id]")!.style
                .overflow === "hidden"
                ? 600
                : 900,
          }) as DOMRect,
      );
      fireEvent.click(less);
      expect(
        screen.getByRole("button", { name: "Show more: Check" }),
      ).not.toBeNull();
      expect(scrollTop).toBe(700);
    } finally {
      // @ts-expect-error removing the jsdom layout stub restores the prototype's own getter
      delete HTMLSpanElement.prototype.scrollHeight;
    }
  });
});
