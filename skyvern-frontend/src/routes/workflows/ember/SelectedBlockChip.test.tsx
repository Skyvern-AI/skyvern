// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test } from "vitest";

import { SelectedBlockChip } from "./SelectedBlockChip";
import { readSelectedBlockLabel } from "./selectedBlockLabel";

afterEach(() => {
  cleanup();
  window.history.pushState(null, "", "/");
});

describe("SelectedBlockChip", () => {
  test("mirrors the canvas selection from the URL", () => {
    render(
      <MemoryRouter initialEntries={["/?selected-block=login"]}>
        <SelectedBlockChip />
      </MemoryRouter>,
    );
    expect(screen.getByText("login")).toBeTruthy();
  });

  test("renders nothing without a selection", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <SelectedBlockChip />
      </MemoryRouter>,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("readSelectedBlockLabel", () => {
  test("reads the live URL", () => {
    window.history.pushState(null, "", "/?selected-block=checkout");
    expect(readSelectedBlockLabel()).toBe("checkout");
  });

  test("null when absent or blank", () => {
    window.history.pushState(null, "", "/");
    expect(readSelectedBlockLabel()).toBeNull();
    window.history.pushState(null, "", "/?selected-block=");
    expect(readSelectedBlockLabel()).toBeNull();
  });

  test("falls back to the router search when the window URL is bare", () => {
    // Memory-router case: window.location.search stays empty.
    expect(readSelectedBlockLabel("?selected-block=login")).toBe("login");
  });
});
