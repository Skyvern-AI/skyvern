// @vitest-environment jsdom
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { TagPickerCommand } from "./TagPickerCommand";
import type { Tag } from "../../types/tagTypes";

// cmdk needs ResizeObserver and scrollIntoView, which jsdom lacks. Install them
// for this suite only and restore afterward so they don't leak into other test
// files sharing the Vitest process.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  Element.prototype.scrollIntoView = () => {};
});

afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});

afterEach(cleanup);

function renderPicker(
  onApply: (tag: Tag) => void,
  props?: Partial<React.ComponentProps<typeof TagPickerCommand>>,
) {
  return render(
    <TagPickerCommand
      tagKeys={[]}
      labelSuggestions={[]}
      valueSuggestionsByKey={new Map()}
      onApply={onApply}
      {...props}
    />,
  );
}

function type(query: string) {
  fireEvent.change(screen.getByPlaceholderText(/label or group:label/i), {
    target: { value: query },
  });
}

describe("TagPickerCommand group-vs-value clarity", () => {
  it("treats a bare entry as a value-only label (key: null)", () => {
    const onApply = vi.fn();
    renderPicker(onApply);

    type("prod");

    const option = screen.getByRole("option");
    expect(option.textContent).toContain("Add prod");
    fireEvent.click(option);

    expect(onApply).toHaveBeenCalledWith({ key: null, value: "prod" });
  });

  it("treats a key:value entry as a grouped label, splitting on the first colon", () => {
    const onApply = vi.fn();
    renderPicker(onApply);

    type("env:prod");

    const option = screen.getByRole("option");
    expect(option.textContent).toContain("Add env: prod");
    fireEvent.click(option);

    expect(onApply).toHaveBeenCalledWith({ key: "env", value: "prod" });
  });

  it("detects a group prefix and offers that group's values, not an Add-label affordance", () => {
    const onApply = vi.fn();
    renderPicker(onApply, {
      tagKeys: [{ key: "env", description: null, workflow_count: 1 }],
      valueSuggestionsByKey: new Map([["env", ["prod", "staging"]]]),
    });

    type("env:");

    // No "Add" affordance: "env:" has no value yet, so it is not a tag to add.
    expect(screen.queryByText(/^Add /)).toBeNull();
    // The group's known values are offered under the group's heading.
    expect(screen.getByText("env:")).toBeTruthy();
    expect(screen.getByText("prod")).toBeTruthy();
    expect(screen.getByText("staging")).toBeTruthy();

    fireEvent.click(screen.getByText("prod"));
    expect(onApply).toHaveBeenCalledWith({ key: "env", value: "prod" });
  });
});

describe("TagPickerCommand disabled state", () => {
  it("does not apply a tag while disabled (guards a bulk apply already in flight)", () => {
    const onApply = vi.fn();
    renderPicker(onApply, { disabled: true });

    type("prod");
    fireEvent.click(screen.getByText(/Add prod/));

    expect(onApply).not.toHaveBeenCalled();
  });
});
