import { type ReactNode } from "react";

import { useWorkflowScopeReadOnly } from "../WorkflowScopeContext";
import { useWorkflowEditorMode } from "../hooks/useWorkflowEditorMode";

function BuildModeOnly({
  children,
  renderInReadOnlyComparison = true,
}: {
  children: ReactNode;
  renderInReadOnlyComparison?: boolean;
}) {
  const mode = useWorkflowEditorMode();
  // Read-only comparison canvases (under /edit, no sidebar) surface a block's prompts inline; opt non-prompt forms out.
  const inReadOnlyComparison =
    useWorkflowScopeReadOnly() && renderInReadOnlyComparison;
  if (mode !== "build" && !inReadOnlyComparison) {
    return null;
  }
  return <>{children}</>;
}

export { BuildModeOnly };
