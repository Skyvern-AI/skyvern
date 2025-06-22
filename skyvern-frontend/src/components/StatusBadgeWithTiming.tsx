import { useEffect, useState } from "react";
import { Status } from "@/api/types";
import { StatusBadge } from "./StatusBadge";
import { formatDistanceStrict } from "date-fns";
import { cn } from "@/util/utils";

type TimingData = {
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

type Props = {
  status: Status;
  timingData?: TimingData | null;
  className?: string;
};

function StatusBadgeWithTiming({ status, timingData, className }: Props) {
  // Force re-render every second for running/queued tasks
  const [, forceUpdate] = useState({});

  useEffect(() => {
    if (status === Status.Running || status === Status.Queued) {
      const interval = setInterval(() => {
        forceUpdate({});
      }, 1000); // Update every second

      return () => clearInterval(interval);
    }
    return undefined;
  }, [status]);

  const calculateDuration = () => {
    if (!timingData) return null;

    // For completed/failed/terminated tasks, show total run time
    if (timingData.started_at && timingData.finished_at) {
      const startedAt = new Date(timingData.started_at);
      const finishedAt = new Date(timingData.finished_at);
      return formatDistanceStrict(finishedAt, startedAt);
    }

    // For running tasks, show elapsed time since start
    if (status === Status.Running && timingData.started_at) {
      const startedAt = new Date(timingData.started_at);
      const now = new Date();
      return formatDistanceStrict(now, startedAt);
    }

    // For queued tasks, show queue time
    if (status === Status.Queued && timingData.queued_at) {
      const queuedAt = new Date(timingData.queued_at);
      const now = new Date();
      return formatDistanceStrict(now, queuedAt);
    }

    return null;
  };

  const duration = calculateDuration();

  return (
    <div className={cn("inline-flex flex-col items-start gap-1", className)}>
      <StatusBadge status={status} />
      {duration && (
        <span className="text-xs text-muted-foreground">
          {status === Status.Running && "Running for "}
          {status === Status.Queued && "Queued for "}
          {duration}
        </span>
      )}
    </div>
  );
}

export { StatusBadgeWithTiming };
