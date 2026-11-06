import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { useDocRunStore } from "./docRunStore";

/** True while the editor pane shows the Doc, so the top bar's Run belongs to it. */
export function useDocRunOwnsTopBar(): boolean {
  const doc = useWorkflowPresentationStore(
    (s) => s.enabled && s.presentation === "doc",
  );
  const yaml = useWorkflowYamlEditorStore((s) => s.active);
  const comparing = useWorkflowPanelStore((s) =>
    Boolean(s.workflowPanelState.data?.showComparison),
  );
  const mounted = useDocRunStore((s) => s.mounted);
  return doc && !yaml && !comparing && mounted;
}
