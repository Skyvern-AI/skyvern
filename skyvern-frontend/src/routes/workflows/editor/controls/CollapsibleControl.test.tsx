// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { CollapsibleControl } from "./CollapsibleControl";

describe("CollapsibleControl", () => {
  afterEach(() => {
    cleanup();
  });

  test("renders children with collapsed classes when show is false", () => {
    const { container } = render(
      <CollapsibleControl show={false}>
        <button>child</button>
      </CollapsibleControl>,
    );
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain("max-h-0");
    expect(wrapper.className).toContain("opacity-0");
    expect(wrapper.className).toContain("pointer-events-none");
    expect(wrapper.getAttribute("aria-hidden")).toBe("true");
  });

  test("renders children with expanded classes when show is true", () => {
    const { container } = render(
      <CollapsibleControl show>
        <button>child</button>
      </CollapsibleControl>,
    );
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain("max-h-9");
    expect(wrapper.className).toContain("opacity-100");
    expect(wrapper.className).not.toContain("pointer-events-none");
    expect(wrapper.getAttribute("aria-hidden")).toBe("false");
  });
});
