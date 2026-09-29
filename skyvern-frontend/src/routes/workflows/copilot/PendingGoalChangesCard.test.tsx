// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";

import { PendingGoalChangesCard } from "./PendingGoalChangesCard";

afterEach(() => {
  cleanup();
  useCopilotActionStore.setState({
    pendingGoalChanges: [],
    pendingBuild: null,
    generatingBlockLabel: null,
    queuedBuilds: [],
    undoGoalChange: () => {},
  });
});

describe("PendingGoalChangesCard", () => {
  test("renders nothing when no Goal change is pending", () => {
    render(<PendingGoalChangesCard />);

    expect(
      screen.queryByRole("region", { name: "Goal changes not applied" }),
    ).toBeNull();
  });

  test("shows the old and new Goal and applies the new one on request", () => {
    useCopilotActionStore.setState({
      pendingGoalChanges: [
        {
          label: "find_provider",
          goal: "Return every provider's phone number",
          previousGoal: "Return the first provider's address",
        },
      ],
    });
    render(<PendingGoalChangesCard />);

    expect(
      screen.getByText("Return the first provider's address"),
    ).toBeTruthy();
    expect(
      screen.getByText("Return every provider's phone number"),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Apply new Goal" }));

    expect(useCopilotActionStore.getState().pendingBuild).toEqual({
      blockLabel: "find_provider",
      prompt: "Return every provider's phone number",
      applyingGoalChange: true,
    });
  });

  test("applies several Goal changes one after another", () => {
    useCopilotActionStore.setState({
      pendingGoalChanges: [
        { label: "first", goal: "Goal one", previousGoal: "Old one" },
        { label: "second", goal: "Goal two", previousGoal: "Old two" },
      ],
    });
    render(<PendingGoalChangesCard />);

    fireEvent.click(screen.getByRole("button", { name: "Apply new Goals" }));

    const state = useCopilotActionStore.getState();
    expect(state.pendingBuild?.blockLabel).toBe("first");
    expect(state.queuedBuilds.map((queued) => queued.blockLabel)).toEqual([
      "second",
    ]);
    expect(screen.getByText("Applying the new Goal…")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Apply new Goals" }),
    ).toBeNull();
  });

  test("Undo asks the canvas to restore that block's old Goal", () => {
    const undoGoalChange = vi.fn();
    useCopilotActionStore.setState({
      pendingGoalChanges: [
        { label: "find_provider", goal: "New", previousGoal: "Old" },
      ],
      undoGoalChange,
    });
    render(<PendingGoalChangesCard />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "Undo the Goal change on find_provider",
      }),
    );

    expect(undoGoalChange).toHaveBeenCalledWith("find_provider");
  });
});
