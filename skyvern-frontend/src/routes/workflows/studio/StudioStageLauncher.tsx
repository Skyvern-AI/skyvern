import { Button } from "@/components/ui/button";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { STUDIO_PANE_META } from "./paneMeta";
import { STUDIO_PANE_IDS, type StudioPaneId } from "./panes";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunSignals } from "./useStudioRunSignals";

/**
 * Zero-panes stage: every pane stays one labeled click away instead of the
 * stage dead-ending. Mirrors the spine's Timeline gating.
 */
export function StudioStageLauncher() {
  const { openPane } = useStudioPanes();
  const { hasRun } = useStudioRunSignals();
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
            const disabled = id === "timeline" && !hasRun;
            return (
              <Button
                key={id}
                type="button"
                variant="secondary"
                size="sm"
                disabled={disabled}
                title={disabled ? "Timeline: no runs yet" : `Open ${label}`}
                onClick={() => open(id)}
                className="gap-2"
              >
                <Icon className="size-4" aria-hidden />
                {label}
                {/* title isn't exposed on a disabled control; keep the reason
                    readable in browse mode. */}
                {disabled ? (
                  <span className="sr-only">(no runs yet)</span>
                ) : null}
              </Button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
