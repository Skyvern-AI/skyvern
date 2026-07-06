import { useEffect, useRef } from "react";

import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

type CommitYaml = (persist?: boolean) => Promise<boolean>;

// Registers a stable wrapper so the YAML-aware save paths and the overlay's
// Visual toggle always call the owning editor's latest commit closure. The
// store is global, so unmount must close() it — otherwise a dirty draft leaks
// into the next workflow's editor (Workspace remounts per workflow id).
export function useWorkflowYamlEditorLifecycle(commitYaml: CommitYaml): void {
  const commitYamlRef = useRef(commitYaml);
  commitYamlRef.current = commitYaml;
  const registerCommit = useWorkflowYamlEditorStore((s) => s.registerCommit);
  useEffect(() => {
    const commit = (persist?: boolean) => commitYamlRef.current(persist);
    registerCommit(commit);
    return () => {
      registerCommit(null);
      useWorkflowYamlEditorStore.getState().close();
    };
  }, [registerCommit]);
}
