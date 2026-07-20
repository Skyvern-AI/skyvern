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
