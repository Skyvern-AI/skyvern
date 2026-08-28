import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { WorkingExampleInspector } from "./WorkingExampleInspector";

it("shows provenance, structure, playback, and result before one safe copy", () => {
  const onMakeCopy = vi.fn();
  render(<WorkingExampleInspector isPending={false} onMakeCopy={onMakeCopy} />);
  expect(screen.getByText("Example data, not your run")).toBeTruthy();
  expect(
    screen.getByRole("heading", { name: "Workflow structure" }),
  ).toBeTruthy();
  expect(screen.getByText("Visit a public page")).toBeTruthy();
  expect(
    screen.getByRole("heading", { name: "Example playback" }),
  ).toBeTruthy();
  expect(
    screen.getByText("Opened https://www.skyvern.com/ in this static example."),
  ).toBeTruthy();
  expect(
    screen.getByRole("heading", { name: "Synthetic example result" }),
  ).toBeTruthy();
  expect(screen.getByText("Product summary")).toBeTruthy();
  expect(onMakeCopy).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Make a copy" }));
  expect(onMakeCopy).toHaveBeenCalledTimes(1);
});

it("disables a pending copy and prevents duplicate callback", () => {
  const onMakeCopy = vi.fn();
  render(<WorkingExampleInspector isPending onMakeCopy={onMakeCopy} />);
  const button = screen.getByRole("button", { name: "Make a copy" });
  expect(button.hasAttribute("disabled")).toBe(true);
  fireEvent.click(button);
  expect(onMakeCopy).not.toHaveBeenCalled();
});
