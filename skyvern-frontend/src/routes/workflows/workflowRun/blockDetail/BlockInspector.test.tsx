// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { JsonExplorer } from "./BlockInspector";

afterEach(() => {
  cleanup();
});

describe("JsonExplorer", () => {
  it("previews nested objects with key/value content instead of object counts", () => {
    render(
      <JsonExplorer
        rootLabel="output"
        value={[
          [
            {
              loop_value: "/runs",
              output_parameter: { name: "run_id" },
              output_value: { id: "wr_1" },
            },
          ],
        ]}
      />,
    );

    expect(screen.queryByText(/Object\(/)).toBeNull();
    expect(screen.getByText(/loop_value: "\/runs"/)).toBeDefined();
    expect(
      screen.getByText(/output_parameter: \{ name: "run_id" \}/),
    ).toBeDefined();
  });

  it("hides compact previews once an expandable section is open", () => {
    render(
      <JsonExplorer
        rootLabel="output"
        value={[
          {
            loop_value: "/runs",
            output_parameter: { name: "run_id" },
          },
        ]}
      />,
    );

    const row = screen.getByRole("button", {
      name: /0.*loop_value.*\/runs/i,
    });
    expect(row.textContent).toContain('loop_value: "/runs"');

    fireEvent.click(row);

    expect(row.textContent).not.toContain('loop_value: "/runs"');
    expect(screen.getByText("loop_value")).toBeDefined();
  });
});
