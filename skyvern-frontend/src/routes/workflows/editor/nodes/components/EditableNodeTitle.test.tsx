// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { EditableNodeTitle } from "./EditableNodeTitle";

afterEach(() => {
  cleanup();
});

describe("EditableNodeTitle edit-mode input sizing", () => {
  test("caps the resizing input at its container width so a long label can't overflow into siblings", () => {
    const longValue =
      "This is a deliberately long block label used to reproduce the input overflow bug";
    render(
      <EditableNodeTitle value={longValue} editable onChange={() => {}} />,
    );

    fireEvent.click(screen.getByText(longValue));

    const input = screen.getByDisplayValue(longValue);
    expect(input.className).toContain("max-w-full");
  });

  test("still applies caller padding classes alongside the width cap", () => {
    render(
      <EditableNodeTitle
        value="Set page URL"
        editable
        onChange={() => {}}
        inputClassName="px-2 text-base"
      />,
    );

    fireEvent.click(screen.getByText("Set page URL"));

    const input = screen.getByDisplayValue("Set page URL");
    expect(input.className).toContain("max-w-full");
    expect(input.className).toContain("px-2");
  });

  test("passes through a relative/left offset alongside padding, for callers that align edit-mode text without shrinking their own auto-width column via margin", () => {
    render(
      <EditableNodeTitle
        value="block_1"
        editable
        onChange={() => {}}
        inputClassName="relative -left-1 px-1 text-base"
      />,
    );

    fireEvent.click(screen.getByText("block_1"));

    const input = screen.getByDisplayValue("block_1");
    expect(input.className).toContain("relative");
    expect(input.className).toContain("-left-1");
    expect(input.className).toContain("px-1");
  });
});

describe("EditableNodeTitle renderIdle seam", () => {
  test("without renderIdle, idle still renders the default click-to-edit heading (other consumers unchanged)", () => {
    render(<EditableNodeTitle value="Foo" editable onChange={() => {}} />);

    expect(screen.getByRole("heading", { name: "Foo" })).toBeTruthy();
  });

  test("with renderIdle, the custom idle replaces the heading and startEditing enters the shared input and commits", () => {
    const onChange = vi.fn();
    render(
      <EditableNodeTitle
        value="Foo"
        editable
        onChange={onChange}
        renderIdle={({ startEditing }) => (
          <button onClick={startEditing}>go-edit</button>
        )}
      />,
    );

    // default heading is suppressed for renderIdle callers
    expect(screen.queryByRole("heading")).toBeNull();

    fireEvent.click(screen.getByText("go-edit"));

    const input = screen.getByDisplayValue("Foo");
    fireEvent.change(input, { target: { value: "Bar" } });
    fireEvent.blur(input);

    expect(onChange).toHaveBeenCalledWith("Bar");
  });
});
