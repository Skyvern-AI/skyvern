// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { PendingGoalChangesCard } from "./PendingGoalChangesCard";

afterEach(() => {
  cleanup();
  useCopilotActionStore.setState({
    pendingGoalChanges: [],
    pendingBuild: null,
    generatingBlockLabel: null,
    queuedBuilds: [],
    undoGoalChange: () => {},
    codeEditedBlocks: [],
    suggestingGoalLabels: [],
    readOnlyGoalLabels: [],
  });
  useWorkflowYamlEditorStore.setState({ commitInProgress: false });
});

describe("PendingGoalChangesCard", () => {
  test("renders nothing when no Goal change is pending", () => {
    render(<PendingGoalChangesCard />);

    expect(
      screen.queryByRole("region", { name: "Goal changes not applied" }),
    ).toBeNull();
  });

  test("offers Keep my code only for a change on a block whose code was edited by hand", () => {
    const keepCode = vi.fn();
    useCopilotActionStore.setState({
      keepCode,
      pendingGoalChanges: [
        {
          label: "find_provider",
          goal: "Return every phone number",
          previousGoal: null,
          codeEditedByHand: true,
        },
        {
          label: "find_clinic",
          goal: "Return every clinic",
          previousGoal: null,
        },
      ],
    });
    render(<PendingGoalChangesCard />);

    const buttons = screen.getAllByRole("button", { name: /^Keep my code/ });
    expect(buttons.length).toBe(1);
    fireEvent.click(buttons[0]!);

    expect(keepCode).toHaveBeenCalledWith("find_provider");
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

describe("PendingGoalChangesCard after a hand code edit", () => {
  const updateGoal = vi.fn();
  const keepGoal = vi.fn();
  const acceptGoal = vi.fn();

  test("says the Goal may be out of date and offers Update Goal and Keep Goal", () => {
    useCopilotActionStore.setState({
      codeEditedBlocks: [
        { label: "read_total", goal: "Read the total", suggestedGoal: null },
      ],
      updateGoal,
      keepGoal,
    });
    render(<PendingGoalChangesCard />);

    expect(
      screen.getByRole("status", { name: "Code changed by hand" }).textContent,
    ).toContain("Goal may be out of date");
    fireEvent.click(
      screen.getByRole("button", { name: "Update Goal for read_total" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Keep Goal for read_total" }),
    );

    expect(updateGoal).toHaveBeenCalledWith("read_total");
    expect(keepGoal).toHaveBeenCalledWith("read_total");
  });

  test("shows the suggestion against the current Goal and accepts it on request", () => {
    useCopilotActionStore.setState({
      codeEditedBlocks: [
        {
          label: "read_total",
          goal: "Read the total",
          suggestedGoal: "The order page shows its total and currency.",
        },
      ],
      acceptGoal,
    });
    render(<PendingGoalChangesCard />);

    expect(screen.getByText("Read the total")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Accept the suggested Goal for read_total",
      }),
    );

    expect(acceptGoal).toHaveBeenCalledWith("read_total");
  });

  test.each([
    ["the block is building", { generatingBlockLabel: "read_total" }, false],
    ["the canvas is read-only", { readOnlyGoalLabels: ["read_total"] }, false],
    ["a YAML commit is in progress", {}, true],
  ])(
    "disables Update, Accept, Keep and Undo while %s",
    (_, state, commitInProgress) => {
      useWorkflowYamlEditorStore.setState({ commitInProgress });
      for (const suggestedGoal of [null, "Suggested"]) {
        useCopilotActionStore.setState({
          ...state,
          codeEditedBlocks: [
            { label: "read_total", goal: "Read the total", suggestedGoal },
          ],
          pendingGoalChanges: [
            { label: "read_total", goal: "New", previousGoal: "Old" },
          ],
        });
        render(<PendingGoalChangesCard />);

        const goalButtons = screen
          .getAllByRole("button")
          .filter((button) => !button.textContent?.startsWith("Apply"));
        expect(goalButtons.length).toBeGreaterThanOrEqual(2);
        for (const button of goalButtons) {
          expect((button as HTMLButtonElement).disabled).toBe(true);
        }
        cleanup();
      }
    },
  );
});
