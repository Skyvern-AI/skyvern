import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { Collapsible } from "@/components/ui/collapsible";

import { CollapseContext } from "./CollapseContext";
import { NodeBody } from "./NodeBody";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderInRoot(open: boolean) {
  return render(
    <CollapseContext.Provider value={{ open }}>
      <Collapsible open={open}>
        <NodeBody>
          <div data-testid="body">body content</div>
        </NodeBody>
      </Collapsible>
    </CollapseContext.Provider>,
  );
}

function NullBody() {
  return null;
}

describe("NodeBody", () => {
  test("renders content with data-state=open when open", () => {
    renderInRoot(true);
    const body = screen.getByTestId("body");
    expect(body).toBeTruthy();
    const wrapper = body.closest("[data-state]")!;
    expect(wrapper.getAttribute("data-state")).toBe("open");
  });

  test("unmounts content when closed (no forceMount)", () => {
    renderInRoot(false);
    expect(screen.queryByTestId("body")).toBeNull();
  });

  test("applies the collapsible animation classes", () => {
    renderInRoot(true);
    const wrapper = screen.getByTestId("body").closest("[data-state]")!;
    expect(wrapper.className).toContain(
      "data-[state=open]:animate-collapsible-down",
    );
    expect(wrapper.className).toContain(
      "data-[state=closed]:animate-collapsible-up",
    );
    expect(wrapper.className).toContain("overflow-hidden");
  });

  test("preserves an inline transform on the body element across collapsible-down animationend", () => {
    const { container } = render(
      <Collapsible open>
        <NodeBody style={{ transform: "rotate(5deg)" }}>
          <p>body</p>
        </NodeBody>
      </Collapsible>,
    );

    const body = container
      .querySelector("p")!
      .closest("[data-state]") as HTMLElement;
    expect(body).not.toBeNull();
    expect(body.style.transform).toBe("rotate(5deg)");
    const recompositeWrapper = body.querySelector(
      "[data-node-body-recomposite]",
    ) as HTMLElement;
    expect(recompositeWrapper).not.toBeNull();

    vi.stubGlobal("CSS", { escape: (value: string) => value });
    const originalOffsetHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "offsetHeight",
    );
    let recomposedElement: HTMLElement | null = null;
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        recomposedElement = this as HTMLElement;
        return 0;
      },
    });

    try {
      // Simulate the Radix collapsible-down animation ending.
      const event = new Event("webkitAnimationEnd", {
        bubbles: true,
        cancelable: true,
      });
      Object.defineProperty(event, "animationName", {
        value: "collapsible-down",
      });
      fireEvent(body, event);
    } finally {
      if (originalOffsetHeight) {
        Object.defineProperty(
          HTMLElement.prototype,
          "offsetHeight",
          originalOffsetHeight,
        );
      } else {
        delete (HTMLElement.prototype as { offsetHeight?: number })
          .offsetHeight;
      }
    }

    // After the recomposite kick fires, the caller's transform must still be present.
    expect(body.style.transform).toBe("rotate(5deg)");
    expect(recomposedElement).toBe(recompositeWrapper);
    expect(recomposedElement).not.toBe(body);
  });

  test("recomposes on accordion-down from a nested accordion", () => {
    const { container } = render(
      <Collapsible open>
        <NodeBody>
          <div data-testid="accordion-content">accordion child</div>
        </NodeBody>
      </Collapsible>,
    );

    const body = container
      .querySelector("[data-testid='accordion-content']")!
      .closest("[data-state]") as HTMLElement;
    const recompositeWrapper = body.querySelector(
      "[data-node-body-recomposite]",
    ) as HTMLElement;
    const accordionChild = container.querySelector(
      "[data-testid='accordion-content']",
    ) as HTMLElement;

    const originalOffsetHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "offsetHeight",
    );
    let recomposedElement: HTMLElement | null = null;
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        recomposedElement = this as HTMLElement;
        return 0;
      },
    });

    try {
      const event = new Event("webkitAnimationEnd", {
        bubbles: true,
        cancelable: true,
      });
      Object.defineProperty(event, "animationName", {
        value: "accordion-down",
      });
      fireEvent(accordionChild, event);
    } finally {
      if (originalOffsetHeight) {
        Object.defineProperty(
          HTMLElement.prototype,
          "offsetHeight",
          originalOffsetHeight,
        );
      } else {
        delete (HTMLElement.prototype as { offsetHeight?: number })
          .offsetHeight;
      }
    }

    expect(recomposedElement).toBe(recompositeWrapper);
  });

  test("ignores unrelated animation names", () => {
    const { container } = render(
      <Collapsible open>
        <NodeBody>
          <div data-testid="inner">inner</div>
        </NodeBody>
      </Collapsible>,
    );

    const body = container
      .querySelector("[data-testid='inner']")!
      .closest("[data-state]") as HTMLElement;

    const originalOffsetHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "offsetHeight",
    );
    let recomposedElement: HTMLElement | null = null;
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        recomposedElement = this as HTMLElement;
        return 0;
      },
    });

    try {
      const event = new Event("webkitAnimationEnd", {
        bubbles: true,
        cancelable: true,
      });
      Object.defineProperty(event, "animationName", {
        value: "fade-in",
      });
      fireEvent(body, event);
    } finally {
      if (originalOffsetHeight) {
        Object.defineProperty(
          HTMLElement.prototype,
          "offsetHeight",
          originalOffsetHeight,
        );
      } else {
        delete (HTMLElement.prototype as { offsetHeight?: number })
          .offsetHeight;
      }
    }

    expect(recomposedElement).toBeNull();
  });

  test("keeps the outer body empty when a child component renders null", () => {
    const { container } = render(
      <Collapsible open>
        <NodeBody>
          <NullBody />
        </NodeBody>
      </Collapsible>,
    );

    const body = Array.from(container.querySelectorAll("[data-state]")).find(
      (element) => element.className.includes("overflow-hidden"),
    ) as HTMLElement;
    expect(body).not.toBeNull();
    expect(body.className).toContain("empty:hidden");
    expect(body.className).toContain(
      "[&:has(>[data-node-body-recomposite]:empty)]:hidden",
    );
    const recompositeWrapper = body.querySelector(
      "[data-node-body-recomposite]",
    ) as HTMLElement;
    expect(recompositeWrapper).not.toBeNull();
    expect(recompositeWrapper.childNodes.length).toBe(0);
  });
});
