// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { RunFieldValue } from "./RunFieldValue";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RunFieldValue", () => {
  test("clamps a long primitive value, toggles it via Show more / Show less, and offers copy", () => {
    // jsdom has no layout engine, so mock the clamp overflow the component
    // measures (scrollHeight > clientHeight).
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(96);

    const value = "A synthetic field value long enough to overflow the clamp.";
    render(<RunFieldValue value={value} label="doc" />);

    // Collapsed: the value is clamped and a copy affordance is present.
    expect(screen.getByText(value).className).toContain("line-clamp-6");
    expect(
      screen.queryByRole("button", { name: "Copy to clipboard" }),
    ).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.queryByRole("button", { name: "Show less" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    // Expanded: the clamp is dropped so the full value is visible.
    expect(screen.getByText(value).className).not.toContain("line-clamp-6");
  });

  test("renders nested objects as the collapsible searchable tree, not clamped prose", () => {
    render(<RunFieldValue value={{ count: 3 }} label="line_items" />);
    expect(screen.queryByPlaceholderText("Search JSON")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  test("shows a muted placeholder for nullish/empty primitives but keeps the literal string 'null' as text", () => {
    for (const absent of [null, undefined, ""]) {
      render(<RunFieldValue value={absent} label="field" />);
      // The em-dash placeholder replaces the copyable prose box.
      expect(screen.getByText("—")).not.toBeNull();
      expect(
        screen.queryByRole("button", { name: "Copy to clipboard" }),
      ).toBeNull();
      cleanup();
    }

    // "null" is a real string value, not an absent one: prose + copy, no dash.
    render(<RunFieldValue value="null" label="field" />);
    expect(screen.getByText("null")).not.toBeNull();
    expect(screen.queryByText("—")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Copy to clipboard" }),
    ).not.toBeNull();
  });
});
