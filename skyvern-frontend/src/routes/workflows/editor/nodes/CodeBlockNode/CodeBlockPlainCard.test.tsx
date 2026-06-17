// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { CodeBlockStep } from "@/routes/workflows/types/workflowTypes";

import { CodeBlockPlainCard } from "./CodeBlockPlainCard";

afterEach(() => {
  cleanup();
});

describe("CodeBlockPlainCard", () => {
  it("renders a page.evaluate step with the Execute JS label, not a dropped or blank step", () => {
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "goto_url",
        description: "Open https://news.ycombinator.com/",
        line_start: 1,
        line_end: 1,
      },
      {
        action_type: "execute_js",
        description: "Run a script",
        line_start: 3,
        line_end: 3,
      },
    ];

    render(<CodeBlockPlainCard steps={steps} />);

    expect(screen.getByText("Goto URL")).toBeDefined();
    expect(screen.getByText("Execute JS")).toBeDefined();
    expect(screen.getByText("Run a script")).toBeDefined();
    // The evaluate step is the 2nd of 2 — the count must match the script, not drop it.
    expect(screen.getByText("2")).toBeDefined();
  });

  it("humanizes an unmapped step action type instead of rendering a blank label", () => {
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "go_forward",
        description: "Go forward",
        line_start: 1,
        line_end: 1,
      },
    ];

    render(<CodeBlockPlainCard steps={steps} />);

    expect(screen.getByText("Go Forward")).toBeDefined();
  });
});
