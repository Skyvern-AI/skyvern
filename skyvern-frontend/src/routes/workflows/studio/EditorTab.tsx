import { type ComponentProps, type CSSProperties } from "react";

import { Workspace } from "@/routes/workflows/editor/Workspace";
import {
  RUN_BLOCKING_SURFACE_TOP_VAR,
  RunBlockingSurface,
} from "@/routes/workflows/editor/runValidation/RunBlockingSurface";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

export type StudioWorkspaceProps = Omit<
  ComponentProps<typeof Workspace>,
  "showBrowser" | "embedded"
>;

// Editor tab: the real Workspace canvas in embedded mode (the shell owns the
// top bar and pane chrome, so Workspace's floating header is suppressed).
export function EditorTab(props: StudioWorkspaceProps) {
  const showComparison = useWorkflowPanelStore(
    (s) => s.workflowPanelState.data?.showComparison,
  );
  return (
    <div
      className="relative h-full w-full"
      style={{ [RUN_BLOCKING_SURFACE_TOP_VAR]: "0.75rem" } as CSSProperties}
    >
      <Workspace {...props} showBrowser={false} embedded />
      {showComparison ? null : <RunBlockingSurface />}
    </div>
  );
}
