// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { AppNode } from "../nodes";
import type { DocEditApi } from "./docEditApi";
import { DocOneActionHint } from "./DocOneActionHint";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

afterEach(cleanup);

const node = (id: string, goal: string) =>
  ({
    id,
    type: "action",
    data: { navigationGoal: goal },
  }) as unknown as AppNode;

function renderHint(n: AppNode) {
  const turnInto = vi.fn();
  return {
    turnInto,
    ...render(
      <WorkflowDocActionsContext.Provider
        value={
          {
            editable: true,
            edit: { turnInto } as unknown as DocEditApi,
          } as never
        }
      >
        <DocOneActionHint node={n} />
      </WorkflowDocActionsContext.Provider>,
    ),
  };
}

test("a plain 'and' between two objects shows no hint, two actions do", () => {
  renderHint(node("a", "Enter username and password"));
  expect(screen.queryByText(/does one action/)).toBeNull();
  cleanup();
  renderHint(node("a", "Search for X and then open the first result"));
  expect(screen.getByText(/does one action/)).toBeTruthy();
});

test("the hint can be dismissed, and stays dismissed when the step remounts", () => {
  const goal = "Search for X and then open the first result";
  renderHint(node("dismiss-me", goal));
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByText(/does one action/)).toBeNull();
  cleanup();
  renderHint(node("dismiss-me", goal));
  expect(screen.queryByText(/does one action/)).toBeNull();
  cleanup();
  renderHint(node("other-step", goal));
  expect(screen.getByText(/does one action/)).toBeTruthy();
});
