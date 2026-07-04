import { Button } from "@/components/ui/button";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { ControlTooltip } from "./ControlTooltip";
import { STUDIO_PANE_META } from "./paneMeta";
import {
  DELETED_WORKFLOW_BLOCKED_PANES,
  STUDIO_PANE_IDS,
  type StudioPaneId,
} from "./panes";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunSignals } from "./useStudioRunSignals";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

/**
 * Zero-panes stage: every pane stays one labeled click away instead of the
 * stage dead-ending. Mirrors the top-bar toggles' Overview gating.
 */
export function StudioStageLauncher() {
  const { openPane } = useStudioPanes();
  const { hasRun } = useStudioRunSignals();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);

  const open = (id: StudioPaneId) => {
    if (id === "browser") {
      clearBrowserActivity();
    }
    openPane(id);
  };

  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <p className="text-sm text-muted-foreground">
          All panes are closed. Open one to keep working:
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          {STUDIO_PANE_IDS.map((id) => {
            const { label, icon: Icon } = STUDIO_PANE_META[id];
            const blockedByDeletion =
              workflowDeleted && DELETED_WORKFLOW_BLOCKED_PANES.includes(id);
            const disabled =
              (id === "overview" && !hasRun) || blockedByDeletion;
            const reason = blockedByDeletion
              ? "Source agent deleted"
              : "Overview: no runs yet";
            const tile = (
              <Button
                key={id}
                type="button"
                variant="secondary"
                size="sm"
                disabled={disabled}
                onClick={() => open(id)}
                className="gap-2"
              >
                <Icon className="size-4" aria-hidden />
                {label}
                {/* The tooltip isn't exposed on a disabled control in browse
                    mode; keep the reason readable there. */}
                {disabled ? (
                  <span className="sr-only">
                    (
                    {blockedByDeletion ? "source agent deleted" : "no runs yet"}
                    )
                  </span>
                ) : null}
              </Button>
            );
            // Labelled tiles are self-describing; only a disabled tile
            // tooltips (its reason).
            if (!disabled) {
              return tile;
            }
            return (
              <ControlTooltip key={id} content={reason} blocked>
                {tile}
              </ControlTooltip>
            );
          })}
        </div>
      </div>
    </div>
  );
}
