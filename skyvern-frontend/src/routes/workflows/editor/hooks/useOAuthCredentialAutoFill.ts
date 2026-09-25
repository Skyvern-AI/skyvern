import { useEffect, useRef } from "react";
import { deferredEdits } from "@/hooks/useDeferredLockedEdit";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowScopeId } from "../WorkflowScopeContext";

export function useOAuthCredentialAutoFill({
  nodeId,
  field,
  value,
  firstValidId,
  needsAutoFill,
  isLoading,
  isFetching,
  onChange,
}: {
  nodeId: string;
  field: string;
  value: string;
  firstValidId: string | undefined;
  needsAutoFill: boolean;
  isLoading: boolean;
  isFetching: boolean;
  onChange: (value: string) => void;
}): void {
  const mutationLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const workflowId = useWorkflowScopeId();
  const deferKey = JSON.stringify([workflowId, nodeId, field]);
  // A child unlock effect can consume the buffer before its value renders here.
  const hasPendingEdit = deferredEdits.has(deferKey);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const valueRef = useRef(value);
  valueRef.current = value;

  useEffect(() => {
    if (
      mutationLocked ||
      isLoading ||
      isFetching ||
      !needsAutoFill ||
      !firstValidId ||
      valueRef.current ||
      hasPendingEdit ||
      deferredEdits.has(deferKey)
    ) {
      return;
    }
    onChangeRef.current(firstValidId);
  }, [
    mutationLocked,
    isLoading,
    isFetching,
    needsAutoFill,
    firstValidId,
    hasPendingEdit,
    deferKey,
  ]);
}
