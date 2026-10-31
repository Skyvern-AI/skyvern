import {
  CheckCircledIcon,
  CrossCircledIcon,
  MinusCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

import { Status } from "@/api/types";
import { TerminatedIcon, terminatedTone } from "@/components/terminatedVisual";

export function StatusDot({
  status,
  isFinalized,
}: {
  status: Status | null;
  isFinalized: boolean;
}) {
  const label = status
    ? status.replace("_", " ")
    : isFinalized
      ? "did not execute"
      : "not started";
  const isCompleted = status === Status.Completed;
  const isTerminated = status === Status.Terminated;
  const isFailure =
    status === Status.Failed ||
    status === Status.TimedOut ||
    status === Status.Canceled;
  const isRunning = status === Status.Running && !isFinalized;

  const glyph = (() => {
    if (isCompleted) {
      return <CheckCircledIcon className="size-3.5 shrink-0 text-success" />;
    }
    if (isTerminated) {
      return (
        <TerminatedIcon className={`size-3.5 shrink-0 ${terminatedTone}`} />
      );
    }
    if (isFailure) {
      return (
        <CrossCircledIcon className="size-3.5 shrink-0 text-destructive" />
      );
    }
    if (status === Status.Skipped) {
      return (
        <MinusCircledIcon className="size-3.5 shrink-0 text-muted-foreground" />
      );
    }
    if (isRunning) {
      return (
        <ReloadIcon className="size-3.5 shrink-0 animate-spin text-sky-700 dark:text-sky-400" />
      );
    }
    return (
      <div className="size-2 shrink-0 rounded-full bg-muted-foreground dark:bg-slate-600" />
    );
  })();

  return (
    <span
      title={label}
      role="img"
      aria-label={label}
      className="flex shrink-0 items-center"
    >
      {glyph}
    </span>
  );
}
