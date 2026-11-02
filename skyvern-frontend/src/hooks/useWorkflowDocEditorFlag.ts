import { WORKFLOW_DOC_EDITOR_FLAG } from "@/util/featureFlags";
import { useFeatureFlag } from "./useFeatureFlag";

const overrides = new Set(
  import.meta.env.DEV
    ? String(import.meta.env.VITE_FEATURE_FLAG_OVERRIDES ?? "")
        .split(",")
        .map((flag) => flag.trim())
        .filter(Boolean)
    : [],
);

// Kill switch, on unless the build sets VITE_WORKFLOW_DOC_EDITOR_ENABLED=false. It only makes the Doc view
// available; each person still opts in from Settings, Feature previews.
export function isWorkflowDocEditorEnabled(): boolean {
  return (
    String(import.meta.env.VITE_WORKFLOW_DOC_EDITOR_ENABLED ?? "")
      .trim()
      .toLowerCase() !== "false"
  );
}

export function useWorkflowDocEditorFlag(): boolean {
  const enabled = useFeatureFlag(WORKFLOW_DOC_EDITOR_FLAG);
  return (
    isWorkflowDocEditorEnabled() &&
    (enabled === true || overrides.has(WORKFLOW_DOC_EDITOR_FLAG))
  );
}
