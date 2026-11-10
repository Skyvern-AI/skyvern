// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AppNode } from "../nodes";
import type { DocEditApi } from "./docEditApi";
import { DocOneActionHint } from "./DocOneActionHint";
import type { DocPromptUrlOffer } from "./docPromptUrl";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
    cb();
    return 0;
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const node = (id: string, goal: string) =>
  ({
    id,
    type: "action",
    data: { navigationGoal: goal },
  }) as unknown as AppNode;

function renderHint(n: AppNode, urlOffer: DocPromptUrlOffer | null = null) {
  const turnInto = vi.fn();
  const setFocusId = vi.fn();
  return {
    turnInto,
    setFocusId,
    ...render(
      <WorkflowDocActionsContext.Provider
        value={
          {
            editable: true,
            edit: { turnInto } as unknown as DocEditApi,
            setFocusId,
          } as never
        }
      >
        <DocOneActionHint node={n} urlOffer={urlOffer} />
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

test("a step shows one suggestion at a time: the URL offer waits for the Browser Task hint", () => {
  renderHint(
    node("both", "Go to https://example.com/catalog and search for shoes"),
    { url: "https://example.com/catalog", required: true },
  );
  expect(screen.getByText(/does one action/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /as the URL/ })).toBeNull();
  const hintButton = screen.getByRole("button", {
    name: "Turn into Browser Task",
  });
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.getByText("This step needs a page to start on.")).toBeTruthy();
  // A new button, so focus does not stay on a control whose meaning changed under it.
  const offer = screen.getByRole("button", {
    name: "Use https://example.com/catalog as the URL",
  });
  expect(offer).not.toBe(hintButton);
  expect(hintButton.isConnected).toBe(false);
  expect(offer.getAttribute("aria-describedby")).toBe(
    screen.getByText("This step needs a page to start on.").id,
  );
});
