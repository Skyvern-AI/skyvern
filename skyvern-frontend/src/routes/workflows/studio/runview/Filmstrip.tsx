import { Fragment, useEffect, useRef } from "react";
import { CounterClockwiseClockIcon } from "@radix-ui/react-icons";

import { ReadableActionTypes, Status } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsAFailureType } from "@/routes/tasks/types";
import { actionTypeIcons } from "@/routes/workflows/components/actionTypeIcons";
import { useRunViewStore } from "@/store/RunViewStore";
import { cn } from "@/util/utils";

import { FilmstripFrame } from "../runProjections";
import { FilmstripThumbnail } from "./FilmstripThumbnail";

function frameFailed(status: Status): boolean {
  return statusIsAFailureType({ status }) || status === Status.Canceled;
}

export function Filmstrip({
  frames,
  shownFrameId,
  running,
}: {
  frames: FilmstripFrame[];
  shownFrameId: string | null;
  running: boolean;
}) {
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const stripRef = useRef<HTMLDivElement>(null);

  // Follow the live edge while the user hasn't pinned a frame.
  useEffect(() => {
    if (pinnedFrameId == null && stripRef.current) {
      stripRef.current.scrollLeft = stripRef.current.scrollWidth;
    }
  }, [frames.length, pinnedFrameId]);

  return (
    <div className="shrink-0 overflow-hidden rounded-lg border border-border bg-slate-elevation2">
      <div className="flex items-center justify-between gap-3 px-4 pb-1 pt-2">
        <span className="inline-flex items-center gap-2 text-xs font-semibold text-muted-foreground">
          <CounterClockwiseClockIcon className="h-3.5 w-3.5" />
          Action timeline
        </span>
        <span className="text-[11px] text-muted-foreground/70">
          {frames.length} action{frames.length === 1 ? "" : "s"} · click a frame
          to inspect
        </span>
      </div>
      <div
        ref={stripRef}
        className="flex items-stretch gap-0 overflow-x-auto px-4 pb-3 pt-1"
      >
        {frames.length === 0 && !running ? (
          <div className="px-1 py-4 text-xs text-muted-foreground/70">
            Actions will appear here as the run progresses.
          </div>
        ) : null}
        {frames.map((frame, i) => {
          const active = frame.id === shownFrameId;
          const failed = frameFailed(frame.status);
          return (
            <Fragment key={frame.id}>
              {frame.isBlockStart && i > 0 ? (
                <div
                  className="mx-2 flex w-px shrink-0 items-center self-stretch bg-border"
                  title={frame.blockLabel ?? undefined}
                />
              ) : null}
              <button
                type="button"
                onClick={() => pinFrame(frame.id)}
                aria-label={`Inspect step: ${frame.label}`}
                className={cn(
                  "group flex w-[8.5rem] shrink-0 flex-col gap-1.5 rounded-md border border-transparent p-1.5 text-left transition-colors hover:border-border",
                  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  active &&
                    "border-studio-accent/60 bg-slate-elevation3 ring-1 ring-studio-accent/50",
                  failed &&
                    active &&
                    "border-destructive/60 ring-destructive/50",
                )}
              >
                <div className="relative h-[4.75rem] overflow-hidden rounded bg-slate-elevation3">
                  <FilmstripThumbnail
                    artifactId={frame.screenshotArtifactId}
                    alt={frame.label}
                  />
                  <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[9.5px] font-bold text-white">
                    {frame.index}
                  </span>
                  <span
                    className={cn(
                      "absolute bottom-1 right-1 grid h-5 w-5 place-items-center rounded text-white",
                      failed ? "bg-destructive" : "bg-success",
                    )}
                  >
                    {actionTypeIcons[frame.actionType]}
                  </span>
                </div>
                <div className="truncate text-[11.5px] font-medium text-foreground">
                  {frame.label}
                </div>
                <div className="font-mono text-[10px] text-muted-foreground/70">
                  {ReadableActionTypes[frame.actionType] ?? frame.actionType}
                </div>
              </button>
            </Fragment>
          );
        })}
        {running ? (
          <div className="flex w-[8.5rem] shrink-0 flex-col gap-1.5 p-1.5">
            <Skeleton className="h-[4.75rem] rounded" />
            <Skeleton variant="text" lines={2} className="gap-1.5" />
          </div>
        ) : null}
      </div>
    </div>
  );
}
