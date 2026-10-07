import { ReloadIcon } from "@radix-ui/react-icons";
import { type ComponentProps, type CSSProperties } from "react";

import { Workspace } from "@/routes/workflows/editor/Workspace";
import {
  RUN_BLOCKING_SURFACE_TOP_VAR,
  RunBlockingSurface,
} from "@/routes/workflows/editor/runValidation/RunBlockingSurface";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";

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
  // Show the overlay only while a commit is genuinely in flight or its blocks
  // are landing. Both signals clear on *every* terminal outcome (success, empty,
  // error), so the overlay can never get stuck — unlike `finishRequested`, which
  // stays true on a failed commit to allow a retry.
  const isCommitting = useRecordingStore((s) => s.isCommitting);
  const recordedBlocksPending = useRecordedBlocksStore(
    (s) => (s.blocks?.length ?? 0) > 0,
  );
  const applyingRecording = isCommitting || recordedBlocksPending;

  return (
    <div
      className="relative h-full w-full"
      style={{ [RUN_BLOCKING_SURFACE_TOP_VAR]: "0.75rem" } as CSSProperties}
    >
      <Workspace {...props} showBrowser={false} embedded />
      {showComparison ? null : <RunBlockingSurface />}
      {applyingRecording ? (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-background/70 backdrop-blur-sm">
          <div className="flex items-center gap-3 rounded-lg border bg-slate-elevation2 px-4 py-3 shadow-lg">
            <ReloadIcon className="h-4 w-4 animate-spin text-muted-foreground" />
            <span className="text-sm font-medium text-foreground">
              Adding recorded steps…
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
