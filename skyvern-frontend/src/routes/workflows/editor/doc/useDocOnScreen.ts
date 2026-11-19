import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

/** True while the editor pane shows the Doc. */
export function useDocOnScreen(): boolean {
  const doc = useWorkflowPresentationStore(
    (s) => s.enabled && s.presentation === "doc" && s.docMounted,
  );
  const yaml = useWorkflowYamlEditorStore((s) => s.active);
  const comparing = useWorkflowPanelStore((s) =>
    Boolean(s.workflowPanelState.data?.showComparison),
  );
  return doc && !yaml && !comparing;
}
