// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { RunPlaceholder } from "./RunPlaceholder";

afterEach(cleanup);

describe("RunPlaceholder", () => {
  test("shows the loading copy while a run is loading", () => {
    render(<RunPlaceholder loading />);
    expect(screen.queryByText("Workflow run is loading…")).not.toBeNull();
    expect(
      screen.queryByText("Run the workflow to watch it live here."),
    ).toBeNull();
  });

  test("shows the empty-state copy when no run is loading", () => {
    render(<RunPlaceholder loading={false} />);
    expect(
      screen.queryByText("Run the workflow to watch it live here."),
    ).not.toBeNull();
    expect(screen.queryByText("Workflow run is loading…")).toBeNull();
  });
});
