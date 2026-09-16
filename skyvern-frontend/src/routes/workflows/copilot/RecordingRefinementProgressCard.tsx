import {
  CheckCircledIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useEffect, useState } from "react";

export type RecordingRefinementStatus =
  | "working"
  | "complete"
  | "failed"
  | "cancelled";

type Props = {
  actionCount: number;
  awaitingReview: boolean;
  startedAtMs: number;
  status: RecordingRefinementStatus;
};

function CompletedStep({ children }: { children: string }) {
  return (
    <div className="flex items-center gap-2 text-xs text-foreground">
      <CheckCircledIcon
        aria-hidden="true"
        className="size-4 shrink-0 text-emerald-500"
      />
      <span>{children}</span>
    </div>
  );
}

function RecordingRefinementProgressCard({
  actionCount,
  awaitingReview,
  startedAtMs,
  status,
}: Props) {
  const [elapsedSeconds, setElapsedSeconds] = useState(() =>
    Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000)),
  );
  const working = status === "working";
  const failed = status === "failed" || status === "cancelled";

  useEffect(() => {
    if (!working) {
      return;
    }
    const updateElapsed = () =>
      setElapsedSeconds(
        Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000)),
      );
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [startedAtMs, working]);

  const title = working
    ? "Turning your task demonstration into a workflow"
    : status === "complete"
      ? awaitingReview
        ? "Workflow ready to review"
        : "Workflow refinement complete"
      : "Workflow refinement stopped";

  return (
    <div
      className={`rounded-lg border p-3.5 ${
        status === "complete"
          ? "border-emerald-500/30 bg-emerald-500/[0.06]"
          : failed
            ? "border-red-500/30 bg-red-500/[0.06]"
            : "border-sky-400/35 bg-sky-500/[0.07]"
      }`}
      role="status"
      aria-live="polite"
      data-testid="recording-refinement-progress"
    >
      <div className="flex items-start gap-2.5">
        <div
          className={`mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full ${
            status === "complete"
              ? "bg-emerald-500/15 text-emerald-500"
              : failed
                ? "bg-red-500/15 text-red-500"
                : "bg-sky-500/15 text-sky-500"
          }`}
        >
          {working ? (
            <ReloadIcon
              aria-hidden="true"
              className="size-4 motion-safe:animate-spin motion-reduce:animate-none"
            />
          ) : status === "complete" ? (
            <CheckCircledIcon aria-hidden="true" className="size-4" />
          ) : (
            <CrossCircledIcon aria-hidden="true" className="size-4" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-foreground">
            {title}
          </div>
          <div className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
            {working
              ? `Copilot is reviewing ${actionCount} captured interaction${actionCount === 1 ? "" : "s"} and preparing changes for you to review.`
              : status === "complete"
                ? awaitingReview
                  ? "Review the changes below, then save when you’re ready."
                  : "Copilot finished refining this workflow."
                : status === "cancelled"
                  ? "The refinement was cancelled. Start it again when you’re ready."
                  : "Copilot could not finish the refinement. Review the error below and try again."}
          </div>
        </div>
        {working ? (
          <span
            className="shrink-0 font-mono text-[10.5px] text-muted-foreground"
            aria-hidden="true"
          >
            {elapsedSeconds}s
          </span>
        ) : null}
      </div>

      <div className="ml-9 mt-3 flex flex-col gap-2 border-l border-border/70 pl-3">
        <CompletedStep>Task recording saved</CompletedStep>
        <CompletedStep>Browser interactions prepared</CompletedStep>
        <div
          className={`flex items-center gap-2 text-xs ${
            failed ? "text-red-600 dark:text-red-400" : "text-foreground"
          }`}
        >
          {working ? (
            <ReloadIcon
              aria-hidden="true"
              className="size-4 shrink-0 text-sky-500 motion-safe:animate-spin motion-reduce:animate-none"
            />
          ) : status === "complete" ? (
            <CheckCircledIcon
              aria-hidden="true"
              className="size-4 shrink-0 text-emerald-500"
            />
          ) : (
            <CrossCircledIcon aria-hidden="true" className="size-4 shrink-0" />
          )}
          <span>
            {working
              ? "Refining the workflow…"
              : status === "complete"
                ? "Refined into a reusable workflow"
                : status === "cancelled"
                  ? "Refinement cancelled"
                  : "Refinement needs attention"}
          </span>
        </div>
      </div>
    </div>
  );
}

export { RecordingRefinementProgressCard };
