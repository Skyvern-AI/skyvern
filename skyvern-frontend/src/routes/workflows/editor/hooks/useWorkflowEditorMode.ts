import { useLocation } from "react-router-dom";

type WorkflowEditorMode = "edit" | "build";

// `/studio` (flag on) is the same edit surface legacy served at `/edit`, so both
// map to 'edit'; anything else (`/build`, `/runs`, …) falls back to 'build'.
const EDIT_PATH_PATTERN = /\/(?:edit|studio)(?:$|\/)/;

function useWorkflowEditorMode(): WorkflowEditorMode {
  const { pathname } = useLocation();
  return EDIT_PATH_PATTERN.test(pathname) ? "edit" : "build";
}

export { useWorkflowEditorMode, type WorkflowEditorMode };
