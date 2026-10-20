import { useCallback, useLayoutEffect } from "react";
import {
  deferredEdits,
  recordDeferredEditReplay,
} from "@/hooks/useDeferredLockedEdit";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import {
  isEditorMutationLocked,
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

function useDeferredTitleEdit() {
  const mutationLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const workflowPermanentId = useWorkflowPermanentId();
  const deferKey = `${workflowPermanentId}:title`;

  const onTitleChange = useCallback(
    (title: string) => {
      const titles = useWorkflowTitleStore.getState();
      if (isEditorMutationLocked()) {
        deferredEdits.set(deferKey, {
          value: title,
          propValue: titles.title,
          propChanged: false,
        });
        return;
      }
      deferredEdits.delete(deferKey);
      titles.setTitle(title);
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    },
    [deferKey],
  );

  // Apply user input before the auto-title hook's passive effect can publish
  // a generated title that was also held during the transaction.
  useLayoutEffect(() => {
    if (isEditorMutationLocked()) return;
    const pending = deferredEdits.get(deferKey);
    deferredEdits.delete(deferKey);
    if (
      pending &&
      useWorkflowTitleStore.getState().title === pending.propValue
    ) {
      recordDeferredEditReplay(workflowPermanentId);
      onTitleChange(pending.value);
      // A user can explicitly choose a placeholder name, which still satisfies
      // the generator's isNewTitle check. Invalidate that pending generation too.
      useWorkflowTitleStore.setState((state) => ({
        titleGeneration: state.titleGeneration + 1,
      }));
    }
  }, [deferKey, mutationLocked, onTitleChange, workflowPermanentId]);

  return { mutationLocked, onTitleChange };
}

export { useDeferredTitleEdit };
