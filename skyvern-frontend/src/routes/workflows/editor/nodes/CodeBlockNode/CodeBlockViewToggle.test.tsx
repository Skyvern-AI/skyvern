// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { CodeBlockViewToggle } from "./CodeBlockViewToggle";

afterEach(cleanup);

describe("CodeBlockViewToggle", () => {
  test("marks the active segment and emits the other value on click", () => {
    const onChange = vi.fn();
    render(<CodeBlockViewToggle value="plain" onChange={onChange} />);

    const plain = screen.getByRole("button", { name: "Plain" });
    const code = screen.getByRole("button", { name: /Code/ });
    expect(plain.getAttribute("aria-pressed")).toBe("true");
    expect(code.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(code);
    expect(onChange).toHaveBeenCalledWith("code");
  });

  test("emits plain when the plain segment is clicked from code", () => {
    const onChange = vi.fn();
    render(<CodeBlockViewToggle value="code" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(onChange).toHaveBeenCalledWith("plain");
  });
});
