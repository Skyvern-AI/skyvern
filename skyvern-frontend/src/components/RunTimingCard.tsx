import { Card, CardContent } from "@/components/ui/card";
import { ClockIcon, PlayIcon, TimerIcon } from "@radix-ui/react-icons";
import { formatDistanceStrict } from "date-fns";

type RunTimingData = {
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

type Props = {
  data: RunTimingData;
  className?: string;
};

function RunTimingCard({ data, className }: Props) {
  const calculateQueueTime = () => {
    if (!data.queued_at || !data.started_at) {
      return null;
    }
    const queuedAt = new Date(data.queued_at);
    const startedAt = new Date(data.started_at);
    return formatDistanceStrict(startedAt, queuedAt);
  };

  const calculateRunTime = () => {
    if (!data.started_at || !data.finished_at) {
      return null;
    }
    const startedAt = new Date(data.started_at);
    const finishedAt = new Date(data.finished_at);
    return formatDistanceStrict(finishedAt, startedAt);
  };

  const calculateTotalTime = () => {
    if (!data.queued_at || !data.finished_at) {
      return null;
    }
    const queuedAt = new Date(data.queued_at);
    const finishedAt = new Date(data.finished_at);
    return formatDistanceStrict(finishedAt, queuedAt);
  };

  const queueTime = calculateQueueTime();
  const runTime = calculateRunTime();
  const totalTime = calculateTotalTime();

  // Don't show the card if no timing data is available
  if (!queueTime && !runTime && !totalTime) {
    return null;
  }

  return (
    <Card className={className}>
      <CardContent className="p-4">
        <div className="space-y-3">
          <h3 className="text-sm font-semibold text-slate-300">Run Timing</h3>

          {queueTime && (
            <div className="flex items-center gap-2 text-xs">
              <TimerIcon className="h-4 w-4 text-slate-400" />
              <span className="text-slate-400">Queue Time:</span>
              <span className="font-medium text-slate-200">{queueTime}</span>
            </div>
          )}

          {runTime && (
            <div className="flex items-center gap-2 text-xs">
              <PlayIcon className="h-4 w-4 text-slate-400" />
              <span className="text-slate-400">Run Time:</span>
              <span className="font-medium text-slate-200">{runTime}</span>
            </div>
          )}

          {totalTime && (
            <div className="flex items-center gap-2 text-xs">
              <ClockIcon className="h-4 w-4 text-slate-400" />
              <span className="text-slate-400">Total Time:</span>
              <span className="font-medium text-slate-200">{totalTime}</span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export { RunTimingCard };
