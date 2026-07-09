import { useLocation } from "react-router-dom";

import { useWorkflowScopeReadOnly } from "../WorkflowScopeContext";

type WorkflowEditorMode = "edit" | "build";

// Legacy `/edit` always renders 'edit' (block config in a right-hand sidebar).
// The Studio editor at `/studio` renders 'build' (inline collapsible blocks with
// settings in the start node) — except its read-only comparison canvases, which
// stay 'edit' so the copilot "review changes" diff view is unchanged. Everything
// else (`/build`, `/runs`, …) is 'build'.
const EDIT_PATH_PATTERN = /\/edit(?:$|\/)/;
const STUDIO_PATH_PATTERN = /\/studio(?:$|\/)/;

function useWorkflowEditorMode(): WorkflowEditorMode {
  const { pathname } = useLocation();
  const readOnly = useWorkflowScopeReadOnly();
  if (EDIT_PATH_PATTERN.test(pathname)) {
    return "edit";
  }
  if (STUDIO_PATH_PATTERN.test(pathname)) {
    return readOnly ? "edit" : "build";
  }
  return "build";
}

export { useWorkflowEditorMode, type WorkflowEditorMode };
