import { useEffect, useState } from "react";
import {
  ChevronDownIcon,
  CounterClockwiseClockIcon,
} from "@radix-ui/react-icons";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/util/utils";

import { useRecentActivity } from "./useRecentActivity";
import { RecentActivityList } from "./RecentActivityList";
import { RunStatusGlyph } from "./RunGlyphs";
import { getRunActivityKey, getRunAgoLabel } from "./runActivity";
import type { RecentActivityViewProps } from "./viewProps";

type PopoverPlacement = {
  contentSide?: "top" | "right" | "bottom" | "left";
  contentAlign?: "start" | "center" | "end";
};

function RecentActivityRunSelector({
  contentSide = "bottom",
  contentAlign = "start",
}: PopoverPlacement = {}) {
  const {
    runs,
    currentActivityKey,
    isWorkflowRunning,
    blockTypeByLabel,
    navigateToRun,
  } = useRecentActivity();
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!open) {
      setNow(Date.now());
      return;
    }
    const intervalId = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(intervalId);
  }, [open]);

  if (runs.length === 0) {
    return null;
  }

  const viewProps: RecentActivityViewProps = {
    runs,
    currentActivityKey,
    isWorkflowRunning,
    blockTypeByLabel,
    now,
    onSelect: navigateToRun,
  };
  const current = currentActivityKey
    ? runs.find((run) => getRunActivityKey(run) === currentActivityKey)
    : null;
  const selected = current ?? runs[runs.length - 1];
  const ago = selected ? getRunAgoLabel(selected, viewProps.now) : null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Recent activity — select a run"
          className={cn(
            "flex w-full items-center gap-2 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs text-slate-200 outline-none transition-colors hover:bg-white/10 focus-visible:ring-1 focus-visible:ring-white/40",
          )}
        >
          <CounterClockwiseClockIcon className="size-4 shrink-0 text-slate-400" />
          {selected ? (
            <>
              <RunStatusGlyph
                status={selected.status}
                isWorkflowRunning={isWorkflowRunning}
                className="size-3.5"
              />
              <span className="max-w-[9rem] truncate font-medium">
                {selected.block_label}
              </span>
              {ago && (
                <span className="shrink-0 text-[10px] tabular-nums text-slate-500">
                  {ago}
                </span>
              )}
            </>
          ) : (
            <span className="text-slate-400">Recent activity</span>
          )}
          <span className="shrink-0 text-[10px] tabular-nums text-slate-500">
            · {runs.length} {runs.length === 1 ? "run" : "runs"}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 text-slate-500" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={contentSide}
        align={contentAlign}
        sideOffset={8}
        className="w-auto border-none bg-transparent p-0 shadow-none"
      >
        <RecentActivityList {...viewProps} />
      </PopoverContent>
    </Popover>
  );
}

export { RecentActivityRunSelector };
