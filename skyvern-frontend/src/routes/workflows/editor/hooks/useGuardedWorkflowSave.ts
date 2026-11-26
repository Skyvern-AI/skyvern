import { useIsGlobalWorkflow } from "@/routes/workflows/hooks/useIsGlobalWorkflow";
import { useEmberActionStore } from "@/store/useEmberActionStore";
import {
  SaveRefusedError,
  useWorkflowHasChangesStore,
} from "@/store/WorkflowHasChangesStore";
import {
  selectSaveLockReason,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { useSaveWorkflow } from "./useSaveWorkflow";

export const PENDING_GOAL_SAVE_MESSAGE =
  "Review the pending Ember changes, then save from the editor.";

export function useGuardedWorkflowSave() {
  const isGlobalWorkflow = useIsGlobalWorkflow();
  const saveLockReason = useWorkflowYamlEditorStore(selectSaveLockReason);
  const pendingGoalChanges = useEmberActionStore(
    (state) => state.pendingGoalChanges.length > 0,
  );
  const onSave = useSaveWorkflow();
  const blockedReason = isGlobalWorkflow
    ? "Make a copy of this global agent before saving changes."
    : saveLockReason;
  const assertAllowed = (pendingGoalsReviewed = false) => {
    const reason = isGlobalWorkflow
      ? "Make a copy of this global agent before saving changes."
      : selectSaveLockReason(useWorkflowYamlEditorStore.getState());
    if (reason) throw new SaveRefusedError(reason);
    if (
      !pendingGoalsReviewed &&
      useEmberActionStore.getState().pendingGoalChanges.length > 0
    ) {
      throw new SaveRefusedError(PENDING_GOAL_SAVE_MESSAGE);
    }
  };
  const save = async (options?: {
    pendingGoalsReviewed?: boolean;
    codeCacheDeletionApproved?: boolean;
  }) => {
    assertAllowed(options?.pendingGoalsReviewed);
    if (options?.codeCacheDeletionApproved !== undefined) {
      useWorkflowHasChangesStore
        .getState()
        .setSaidOkToCodeCacheDeletion(options.codeCacheDeletionApproved);
    }
    await onSave();
  };
  return {
    blockedReason,
    saveLockReason,
    pendingGoalChanges,
    assertAllowed,
    save,
  };
}
