import { useEffect, useMemo, useRef } from "react";

import type { CredentialApiResponse } from "@/api/types";
import { deferredEdits } from "@/hooks/useDeferredLockedEdit";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { useWorkflowScopeId } from "../../WorkflowScopeContext";
import { computeLoginGoalPrefill } from "./loginGoalPrefill";

/**
 * Applies the login-goal prefill decision whenever the resolved credential's
 * instructions change — both on an explicit credential selection and when a workflow
 * loads with a credential already selected and the credentials list resolves after mount.
 */
export function useLoginGoalAutoFill({
  nodeId,
  editable,
  selectedCredentialId,
  credentials,
  currentGoal,
  onAutoFill,
}: {
  nodeId: string;
  editable: boolean;
  selectedCredentialId: string | undefined;
  credentials: Array<CredentialApiResponse>;
  currentGoal: string;
  onAutoFill: (goal: string) => void;
}): void {
  const mutationLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const workflowId = useWorkflowScopeId();
  const deferKey = JSON.stringify([workflowId, nodeId, "navigationGoal"]);
  // An unlock effect can remove the buffer before its parent prop update commits.
  const pendingGoalEdit = deferredEdits.get(deferKey);
  const pendingGoalEditRef = useRef(pendingGoalEdit);

  // undefined = credential not yet resolved (or missing) — distinct from null, a
  // resolved credential confirmed to have no instructions.
  const selectedCredentialUserContext = useMemo(() => {
    if (!selectedCredentialId) {
      return undefined;
    }
    const credential = credentials.find(
      (c) => c.credential_id === selectedCredentialId,
    );
    return credential ? (credential.user_context ?? null) : undefined;
  }, [credentials, selectedCredentialId]);

  // Always read the latest onAutoFill/currentGoal even though both are excluded from the
  // effect's deps below — currentGoal to avoid fighting every keystroke, onAutoFill in case a
  // future caller's callback identity changes between renders.
  const onAutoFillRef = useRef(onAutoFill);
  const currentGoalRef = useRef(currentGoal);
  useEffect(() => {
    onAutoFillRef.current = onAutoFill;
    currentGoalRef.current = currentGoal;
    pendingGoalEditRef.current = pendingGoalEdit;
  });

  useEffect(() => {
    if (
      mutationLocked ||
      !editable ||
      selectedCredentialUserContext === undefined ||
      pendingGoalEditRef.current !== undefined ||
      deferredEdits.has(deferKey)
    ) {
      return;
    }
    const prefill = computeLoginGoalPrefill(
      currentGoalRef.current,
      selectedCredentialUserContext,
    );
    if (prefill !== null) {
      onAutoFillRef.current(prefill);
    }
  }, [selectedCredentialUserContext, editable, mutationLocked, deferKey]);
}
