import {
  WORKFLOW_DOC_EDITOR_FLAG,
  WORKFLOW_DOC_IMAGES_FLAG,
} from "@/util/featureFlags";
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

// Screenshots by paste, drop or the picker; off when the flag is absent. Saved screenshots show either way.
export function useWorkflowDocImagesFlag(): boolean {
  const enabled = useFeatureFlag(WORKFLOW_DOC_IMAGES_FLAG);
  return enabled === true || overrides.has(WORKFLOW_DOC_IMAGES_FLAG);
}
