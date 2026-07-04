import {
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { YamlModeToggle } from "../editor/YamlModeToggle";
import { PaneHeaderDivider } from "./PaneHeaderDivider";
import { useStudioPaneCompact } from "./StudioShellContext";

/**
 * Editor pane header chrome: the Visual/Code mode toggle, relocated from the
 * canvas's floating overlay. Entry is registered by the embedded Workspace
 * (it owns the canvas→YAML serialization); exit is the store's shared
 * commit-on-switch flow. Hidden for read-only (global) workflows, which never
 * register an entry point.
 */
export function EditorPaneModeToggle() {
  const compact = useStudioPaneCompact();
  const active = useWorkflowYamlEditorStore((s) => s.active);
  const committing = useWorkflowYamlEditorStore((s) => s.committing);
  const enterYamlMode = useWorkflowYamlEditorStore((s) => s.enterYamlMode);
  if (!enterYamlMode) {
    return null;
  }
  return (
    <>
      <PaneHeaderDivider />
      <YamlModeToggle
        mode={active ? "code" : "visual"}
        compact={compact}
        disabled={committing}
        onCode={enterYamlMode}
        onVisual={() => void commitYamlDraft(false)}
      />
    </>
  );
}
