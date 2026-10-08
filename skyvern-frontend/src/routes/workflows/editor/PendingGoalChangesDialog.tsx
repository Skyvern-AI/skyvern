import { flushSync } from "react-dom";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: () => void;
};

/** Asked on Save when a block's new Goal isn't applied yet: apply it, or undo it and save the rest. */
export function PendingGoalChangesDialog({
  open,
  onOpenChange,
  onSave,
}: Props) {
  const changes = useCopilotActionStore((state) => state.pendingGoalChanges);
  const applyPendingGoalChanges = useCopilotActionStore(
    (state) => state.applyPendingGoalChanges,
  );
  const undoGoalChange = useCopilotActionStore((state) => state.undoGoalChange);

  const single = changes.length === 1;
  const names = changes.map((change) => `“${change.label}”`).join(", ");
  const canUndoAll =
    changes.length > 0 &&
    changes.every((change) => change.previousGoal !== null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {single ? "New Goal not applied" : "New Goals not applied"}
          </DialogTitle>
          <DialogDescription>
            {single
              ? `${names} has a new Goal it doesn't follow yet.`
              : `${names} have new Goals they don't follow yet.`}{" "}
            Apply {single ? "it" : "them"} before saving, or undo the Goal{" "}
            {single ? "change" : "changes"} and save everything else.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          {canUndoAll ? (
            <Button
              variant="secondary"
              onClick={() => {
                // The canvas must render the undo before the save reads it, or the save carries the new Goal.
                flushSync(() => {
                  for (const change of changes) {
                    undoGoalChange(change.label);
                  }
                });
                onOpenChange(false);
                onSave();
              }}
            >
              {single
                ? "Undo Goal change and save"
                : "Undo Goal changes and save"}
            </Button>
          ) : null}
          <Button
            onClick={() => {
              applyPendingGoalChanges();
              onOpenChange(false);
            }}
          >
            {single ? "Apply new Goal" : "Apply new Goals"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
