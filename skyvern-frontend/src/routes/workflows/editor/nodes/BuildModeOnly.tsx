import { type ReactNode } from "react";

import { useWorkflowEditorMode } from "../hooks/useWorkflowEditorMode";

function BuildModeOnly({ children }: { children: ReactNode }) {
  const mode = useWorkflowEditorMode();
  if (mode !== "build") {
    return null;
  }
  return <>{children}</>;
}

export { BuildModeOnly };
