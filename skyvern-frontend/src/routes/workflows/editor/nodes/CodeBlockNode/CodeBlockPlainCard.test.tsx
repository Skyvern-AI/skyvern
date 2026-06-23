// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { CodeBlockStep } from "@/routes/workflows/types/workflowTypes";

import { CodeBlockPlainCard } from "./CodeBlockPlainCard";

afterEach(() => {
  cleanup();
});

describe("CodeBlockPlainCard", () => {
  it("renders a Steps section with a count", () => {
    render(
      <CodeBlockPlainCard
        steps={[
          { action_type: "goto_url", description: "Open the portal" },
          { action_type: "extract", description: "Read the totals" },
        ]}
      />,
    );

    expect(screen.getByText("Steps")).toBeDefined();
    expect(screen.getByText("2 steps")).toBeDefined();
  });

  it("uses a singular label for a single step", () => {
    render(
      <CodeBlockPlainCard
        steps={[{ action_type: "extract", title: "Summarize the page" }]}
      />,
    );

    expect(screen.getByText("1 step")).toBeDefined();
    expect(screen.getByText("Summarize the page")).toBeDefined();
  });

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

    // Per-step action labels.
    expect(screen.getByText("Goto URL")).toBeDefined();
    expect(screen.getByText("Execute JS")).toBeDefined();
    // Plain text for the second step is preserved, not dropped.
    expect(screen.getByText("Run a script")).toBeDefined();
    // The evaluate step is the 2nd of 2 — the count must match the script.
    expect(screen.getByText("2 steps")).toBeDefined();
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

  it("falls back to the readable action type for the step text when title and description are absent", () => {
    const steps: Array<CodeBlockStep> = [
      { action_type: "extract", line_start: 1, line_end: 1 },
    ];

    render(<CodeBlockPlainCard steps={steps} />);

    // The primary step text falls back to the readable type instead of a blank
    // label; the type subtitle reads it too, so it appears twice.
    expect(screen.getAllByText("Extract Data").length).toBe(2);
  });

  it("shows an empty hint when there are no steps", () => {
    render(<CodeBlockPlainCard steps={[]} />);

    expect(screen.getByText(/No steps yet/)).toBeDefined();
  });

  it("shows a generating indicator and stop control while generating", () => {
    render(<CodeBlockPlainCard steps={[]} generating onStop={() => {}} />);

    expect(screen.getByText("Generating…")).toBeDefined();
    expect(screen.getByRole("button", { name: /Stop/ })).toBeDefined();
  });
});
