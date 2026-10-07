import { useEffect, useMemo } from "react";

import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import type { AppNode } from "../nodes";
import { isCredentialParameter } from "../../runValidation";
import { getRunBlockingBlocks } from "./getRunBlockingBlocks";
import { useRunValidationStore } from "./useRunValidationStore";

// Mirrors run-blocking blocks from the live canvas into the shared store; resets on unmount.
export function useSyncRunValidationStore(nodes: Array<AppNode>): void {
  const setBlockingBlocks = useRunValidationStore((s) => s.setBlockingBlocks);
  const parameters = useWorkflowParametersStore((s) => s.parameters);

  // A block stores parameter keys, not types, so the credential check has to
  // resolve them against the workflow's parameter definitions.
  const credentialKeys = useMemo(
    () =>
      new Set(
        parameters
          .filter(isCredentialParameter)
          .map((parameter) => parameter.key),
      ),
    [parameters],
  );

  useEffect(() => {
    setBlockingBlocks(
      getRunBlockingBlocks(nodes, (key) => credentialKeys.has(key)),
    );
  }, [nodes, credentialKeys, setBlockingBlocks]);

  useEffect(() => {
    return () => setBlockingBlocks([]);
  }, [setBlockingBlocks]);
}
