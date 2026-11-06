import { useWorkflowDocEditorFlag } from "@/hooks/useWorkflowDocEditorFlag";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

// Whether the Editor pane is showing the Doc (the inputs the mode toggle reads).
export function useEditorShowsDoc(): boolean {
  const docEnabled = useWorkflowDocEditorFlag();
  const presentation = useWorkflowPresentationStore((s) => s.presentation);
  const comparing = useWorkflowPanelStore((s) =>
    Boolean(s.workflowPanelState.data?.showComparison),
  );
  const yamlActive = useWorkflowYamlEditorStore((s) => s.active);
  const editable = useWorkflowYamlEditorStore((s) => s.enterYamlMode !== null);
  return (
    docEnabled &&
    editable &&
    presentation === "doc" &&
    !comparing &&
    !yamlActive
  );
}
