import { useLocation } from "react-router-dom";

type WorkflowEditorMode = "edit" | "build";

// `/edit` either ends the path or is followed by `/`. This matches the
// React Router definitions in cloud/router.tsx where the workflow editor
// has both bare `/edit` and (potentially future) nested edit subpaths.
// Anything else — including `/build`, run-targeted `/build`, and unrelated
// pages like `/runs` — falls back to 'build'.
const EDIT_PATH_PATTERN = /\/edit(?:$|\/)/;

function useWorkflowEditorMode(): WorkflowEditorMode {
  const { pathname } = useLocation();
  return EDIT_PATH_PATTERN.test(pathname) ? "edit" : "build";
}

export { useWorkflowEditorMode, type WorkflowEditorMode };
