// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, expect, test, vi } from "vitest";

import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";

import type { AppNode } from "./nodes";
import { codeBlockNodeDefaultData } from "./nodes/CodeBlockNode/types";
import { PendingGoalChangesDialog } from "./PendingGoalChangesDialog";
import { PendingGoalChangesPublisher } from "./PendingGoalChangesPublisher";

afterEach(cleanup);

const editedBlock = {
  id: "cb1",
  type: "codeBlock",
  position: { x: 0, y: 0 },
  data: {
    ...codeBlockNodeDefaultData,
    label: "find_provider",
    prompt: "Return every provider's phone number",
    userOwnedGoal: true,
    goalNeedsRegeneration: true,
    goalBeforeEdit: {
      prompt: "Return the first provider's address",
      userOwnedGoal: null,
      goalNeedsRegeneration: null,
    },
  },
} as AppNode;

test("publishes a pending Goal change and undoes it in the canvas", () => {
  render(
    <ReactFlowProvider defaultNodes={[editedBlock]}>
      <PendingGoalChangesPublisher />
    </ReactFlowProvider>,
  );

  expect(useCopilotActionStore.getState().pendingGoalChanges).toEqual([
    {
      label: "find_provider",
      goal: "Return every provider's phone number",
      previousGoal: "Return the first provider's address",
    },
  ]);

  act(() => {
    useCopilotActionStore.getState().undoGoalChange("find_provider");
  });

  expect(useCopilotActionStore.getState().pendingGoalChanges).toEqual([]);
});

test("undo and save saves after the canvas has dropped the new Goal", () => {
  const consoleError = vi.spyOn(console, "error");
  const onOpenChange = vi.fn();
  let changesAtSave: number | undefined;
  const save = vi.fn(() => {
    changesAtSave = useCopilotActionStore.getState().pendingGoalChanges.length;
    // The real save forces buffered edits through with flushSync before it writes.
    flushBufferedEditorEdits();
  });
  render(
    <ReactFlowProvider defaultNodes={[editedBlock]}>
      <PendingGoalChangesPublisher />
      <PendingGoalChangesDialog
        open
        onOpenChange={onOpenChange}
        onSave={save}
      />
    </ReactFlowProvider>,
  );

  fireEvent.click(
    screen.getByRole("button", { name: "Undo Goal change and save" }),
  );

  expect(save).toHaveBeenCalledOnce();
  expect(changesAtSave).toBe(0);
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(consoleError).not.toHaveBeenCalledWith(
    expect.stringContaining("flushSync was called from inside a lifecycle"),
    expect.anything(),
  );
  consoleError.mockRestore();
});
