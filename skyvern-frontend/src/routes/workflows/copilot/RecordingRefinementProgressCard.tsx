import {
  CheckCircledIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

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

function RecordingRefinementProgressCard({
  actionCount,
  awaitingReview,
  status,
}: Props) {
  const working = status === "working";
  const complete = status === "complete";

  return (
    <div
      className="flex flex-col gap-3"
      role="status"
      aria-live="polite"
      data-testid="recording-refinement-progress"
    >
      <div className="flex items-center gap-2.5 rounded-lg border border-border bg-slate-elevation2 p-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-red-500/10">
          <span className="size-2.5 rounded-full border-2 border-red-500" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[12.5px] font-semibold text-foreground">
            Recorded {actionCount} action{actionCount === 1 ? "" : "s"}
          </span>
          <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
            Task demonstration captured
          </span>
        </span>
        {complete ? (
          <CheckCircledIcon className="size-4 shrink-0 text-emerald-500" />
        ) : null}
      </div>

      <p className="pl-1 text-[13px] leading-[1.55] text-foreground">
        {working
          ? "I have the demonstration. I’m turning it into workflow steps now."
          : complete
            ? awaitingReview
              ? "The workflow is ready for you to review."
              : "I finished refining the recording into a reusable workflow."
            : status === "cancelled"
              ? "I stopped refining this recording."
              : "I couldn’t finish refining this recording."}
      </p>

      <div className="flex items-center gap-2 pl-1 text-xs text-muted-foreground">
        {working ? (
          <ReloadIcon className="size-3.5 shrink-0 animate-spin text-violet-500 motion-reduce:animate-none" />
        ) : complete ? (
          <CheckCircledIcon className="size-3.5 shrink-0 text-emerald-500" />
        ) : (
          <CrossCircledIcon className="size-3.5 shrink-0 text-red-500" />
        )}
        <span>
          {working
            ? `Reviewing ${actionCount} recorded action${actionCount === 1 ? "" : "s"}`
            : complete
              ? awaitingReview
                ? "Ready for review"
                : "Refinement complete"
              : status === "cancelled"
                ? "Refinement cancelled"
                : "Refinement needs attention"}
        </span>
      </div>
    </div>
  );
}

export { RecordingRefinementProgressCard };
