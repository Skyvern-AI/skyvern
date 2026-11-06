import {
  COPILOT_ATTENTION_LABEL,
  useCopilotHeaderStore,
} from "@/store/useCopilotHeaderStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { STUDIO_PANE_META, paneLabel } from "./paneMeta";
import { type StudioPaneId } from "./panes";
import { StudioPaneAttentionDot } from "./StudioPaneAttentionDot";

/** Phone-width pane switch: the studio shows one pane, this picks which. */
export function StudioNarrowPaneSwitch({
  ids,
  shown,
  onShow,
}: {
  ids: readonly StudioPaneId[];
  shown: StudioPaneId | undefined;
  onShow: (id: StudioPaneId) => void;
}) {
  const browserActivity = useStudioBrowserStore((s) => s.hasUnseenActivity);
  const copilotAttention = useCopilotHeaderStore((s) => s.attention);
  return (
    <nav
      aria-label="Pane switch"
      data-studio-pane-switch
      className="flex shrink-0 items-center gap-1 border-t border-border bg-slate-elevation2 px-2 pb-[max(0.375rem,env(safe-area-inset-bottom))] pt-1.5"
    >
      {ids.map((id) => {
        const { icon: Icon } = STUDIO_PANE_META[id];
        const active = id === shown;
        // Only another pane's news needs a dot; the shown pane says it itself.
        const activity = id === "browser" && browserActivity && !active;
        const awaiting = id === "copilot" && !active ? copilotAttention : null;
        return (
          <button
            key={id}
            type="button"
            aria-pressed={active}
            aria-label={
              activity
                ? "Browser, new activity"
                : awaiting
                  ? `Copilot, ${COPILOT_ATTENTION_LABEL[awaiting]}`
                  : undefined
            }
            onClick={() => onShow(id)}
            className={cn(
              "inline-flex h-9 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md px-1 text-xs font-medium",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              active
                ? "bg-accent text-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
            )}
          >
            <span className="relative inline-flex">
              <Icon className="size-3.5 shrink-0" aria-hidden />
              {activity ? (
                <StudioPaneAttentionDot
                  tone="activity"
                  title="New browser activity"
                />
              ) : awaiting ? (
                <StudioPaneAttentionDot
                  tone="waiting"
                  title={`Copilot, ${COPILOT_ATTENTION_LABEL[awaiting]}`}
                />
              ) : null}
            </span>
            <span className="truncate">{paneLabel(id)}</span>
          </button>
        );
      })}
    </nav>
  );
}
