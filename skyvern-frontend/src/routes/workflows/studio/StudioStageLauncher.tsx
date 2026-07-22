import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { ControlTooltip } from "./ControlTooltip";
import { PastRunsList } from "./PastRunsList";
import { STUDIO_PANE_META, railLabel } from "./paneMeta";
import {
  DELETED_WORKFLOW_BLOCKED_PANES,
  STUDIO_PANE_IDS,
  type StudioPaneId,
} from "./panes";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

/**
 * Zero-panes stage: every pane stays one labeled click away instead of the
 * stage dead-ending. Copilot/Editor/Browser open their pane; the "Past Runs"
 * tile opens the run selector (picking a run opens the run pane), mirroring the
 * rail tab.
 */
export function StudioStageLauncher() {
  const { openPane } = useStudioPanes();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);
  const [runsSelectorOpen, setRunsSelectorOpen] = useState(false);

  const open = (id: StudioPaneId) => {
    if (id === "browser") {
      clearBrowserActivity();
    }
    openPane(id, { learn: true });
  };

  const onSelectRun = () => {
    openPane("overview", { learn: true });
    setRunsSelectorOpen(false);
  };

  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <p className="text-sm text-muted-foreground">
          All panes are closed. Open one to keep working:
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          {STUDIO_PANE_IDS.map((id) => {
            const { icon: Icon } = STUDIO_PANE_META[id];
            const label = railLabel(id);
            const blockedByDeletion =
              workflowDeleted && DELETED_WORKFLOW_BLOCKED_PANES.includes(id);
            const tile = (
              <Button
                key={id}
                type="button"
                variant="secondary"
                size="sm"
                disabled={blockedByDeletion}
                onClick={id === "overview" ? undefined : () => open(id)}
                className="gap-2"
              >
                <Icon className="size-4" aria-hidden />
                {label}
                {/* The tooltip isn't exposed on a disabled control in browse
                    mode; keep the reason readable there. */}
                {blockedByDeletion ? (
                  <span className="sr-only">(source agent deleted)</span>
                ) : null}
              </Button>
            );

            // The run pane's tile opens the selector; picking a run opens the
            // pane (mirrors the rail tab).
            if (id === "overview") {
              return (
                <Popover
                  key={id}
                  open={runsSelectorOpen}
                  onOpenChange={setRunsSelectorOpen}
                >
                  <PopoverTrigger asChild>{tile}</PopoverTrigger>
                  <PopoverContent
                    align="center"
                    sideOffset={8}
                    className="w-[22rem] p-0"
                  >
                    <PastRunsList
                      open={runsSelectorOpen}
                      onSelect={onSelectRun}
                    />
                  </PopoverContent>
                </Popover>
              );
            }

            // Labelled tiles are self-describing; only a disabled tile
            // tooltips (its reason).
            if (!blockedByDeletion) {
              return tile;
            }
            return (
              <ControlTooltip key={id} content="Source agent deleted" blocked>
                {tile}
              </ControlTooltip>
            );
          })}
        </div>
      </div>
    </div>
  );
}
