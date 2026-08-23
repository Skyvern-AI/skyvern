import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

const { openSearchPanelSpy } = vi.hoisted(() => ({
  openSearchPanelSpy: vi.fn(),
}));

vi.mock("@codemirror/search", async () => {
  const actual =
    await vi.importActual<typeof import("@codemirror/search")>(
      "@codemirror/search",
    );
  return { ...actual, openSearchPanel: openSearchPanelSpy };
});

import { OverviewCodeBlock } from "./OverviewCodeBlock";

test("keeps JSON actions above the CodeMirror search panel", () => {
  render(<OverviewCodeBlock value='{"a":1}' />);

  const toolbar = screen.getByRole("group", { name: "JSON actions" });
  expect(toolbar.className).toContain("justify-end");
  expect(toolbar.className).not.toContain("absolute");
});

test("opens CodeMirror search from the toolbar", () => {
  render(<OverviewCodeBlock value='{"a":1}' />);

  const searchButton = screen.getByRole("button", { name: "Search" });
  expect(searchButton).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Copy to clipboard" }),
  ).toBeTruthy();

  fireEvent.click(searchButton);

  expect(openSearchPanelSpy).toHaveBeenCalledTimes(1);
  expect(openSearchPanelSpy.mock.calls[0]?.[0]).toBeTruthy();
});
