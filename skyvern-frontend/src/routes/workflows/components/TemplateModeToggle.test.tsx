// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TemplateModeToggle } from "./TemplateModeToggle";

afterEach(() => {
  cleanup();
});

describe("TemplateModeToggle", () => {
  it("uses the custom-value label when not pressed and fires onToggle", () => {
    const onToggle = vi.fn();
    render(
      <TemplateModeToggle
        pressed={false}
        pickerTitle="Pick from your spreadsheets"
        onToggle={onToggle}
      />,
    );

    const toggle = screen.getByRole("button", {
      name: "Enter a custom value",
    });
    expect(toggle.getAttribute("title")).toBe("Enter a custom value");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);

    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("uses the picker label when pressed", () => {
    const onToggle = vi.fn();
    render(
      <TemplateModeToggle
        pressed={true}
        pickerTitle="Pick from your spreadsheets"
        onToggle={onToggle}
      />,
    );

    const toggle = screen.getByRole("button", {
      name: "Pick from your spreadsheets",
    });
    expect(toggle.getAttribute("title")).toBe("Pick from your spreadsheets");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(toggle);

    expect(onToggle).toHaveBeenCalledWith(false);
  });
});
