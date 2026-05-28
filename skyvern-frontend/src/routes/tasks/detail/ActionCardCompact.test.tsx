// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type ActionsApiResponse, Status } from "@/api/types";
import { ActionCardCompact } from "./ActionCardCompact";

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_1",
    action_type: "click",
    status: Status.Completed,
    task_id: "task_1",
    step_id: "step_1",
    step_order: 0,
    action_order: 0,
    confidence_float: 0.87,
    description: null,
    reasoning: "Click the submit button",
    intention: null,
    response: null,
    created_by: null,
    text: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("ActionCardCompact", () => {
  it("renders the action type label and reasoning preview", () => {
    render(
      <ActionCardCompact
        action={buildAction()}
        index={3}
        active={false}
        expanded={false}
        onSelect={() => {}}
        onToggleExpanded={() => {}}
      />,
    );

    expect(screen.getByText("Click")).toBeDefined();
    expect(screen.getByText("#3")).toBeDefined();
    expect(screen.getByText("Click the submit button")).toBeDefined();
  });

  it("fires onToggleExpanded when the chevron is clicked and does not fire onSelect", () => {
    const onSelect = vi.fn();
    const onToggleExpanded = vi.fn();
    render(
      <ActionCardCompact
        // chevron only renders when there is hideable detail (input value)
        action={buildAction({
          action_type: "input_text",
          text: "user@example.com",
        })}
        index={1}
        active={false}
        expanded={false}
        onSelect={onSelect}
        onToggleExpanded={onToggleExpanded}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /expand details/i }));

    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("fires onSelect when the row body is clicked", () => {
    const onSelect = vi.fn();
    const onToggleExpanded = vi.fn();
    render(
      <ActionCardCompact
        action={buildAction()}
        index={1}
        active={false}
        expanded={false}
        onSelect={onSelect}
        onToggleExpanded={onToggleExpanded}
      />,
    );

    fireEvent.click(screen.getByText("Click"));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onToggleExpanded).not.toHaveBeenCalled();
  });

  it("renders reasoning inline, input in the expanded panel, and confidence as a chip", () => {
    render(
      <ActionCardCompact
        action={buildAction({
          action_type: "input_text",
          reasoning: "Type the email address",
          text: "user@example.com",
          confidence_float: 0.92,
        })}
        index={2}
        active={false}
        expanded={true}
        onSelect={() => {}}
        onToggleExpanded={() => {}}
      />,
    );

    // Reasoning is always inline now (not duplicated under a "Reasoning" heading)
    expect(screen.getAllByText("Type the email address").length).toBe(1);
    // Input value lives inside the expanded panel
    expect(screen.getByText("Input")).toBeDefined();
    expect(screen.getByText("user@example.com")).toBeDefined();
    // Confidence renders as an inline chip in the top strip — no heading
    expect(screen.getByText("92%")).toBeDefined();
  });

  it("uses response as the input fallback when text is null for input_text", () => {
    render(
      <ActionCardCompact
        action={buildAction({
          action_type: "input_text",
          text: null,
          response: "from-script-value",
        })}
        index={4}
        active={false}
        expanded={true}
        onSelect={() => {}}
        onToggleExpanded={() => {}}
      />,
    );

    expect(screen.getByText("from-script-value")).toBeDefined();
  });
});
