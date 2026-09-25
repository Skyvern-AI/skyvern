import { TrashIcon } from "@radix-ui/react-icons";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import type { WorkflowSchedule } from "@/routes/workflows/types/scheduleTypes";
import { formatNextRun } from "./cronUtils";
import {
  describeCadence,
  getCadenceNextRuns,
  upcomingFirstRun,
} from "./scheduleCadence";
import { cn } from "@/util/utils";

type Props = {
  schedule: WorkflowSchedule;
  isToggling?: boolean;
  onToggle: (scheduleId: string, enabled: boolean) => void;
  onDelete: (scheduleId: string) => void;
};

function ScheduleCard({ schedule, isToggling, onToggle, onDelete }: Props) {
  const humanReadable = describeCadence(schedule);
  const nextRun = getCadenceNextRuns(schedule, schedule.timezone, 1)[0];
  const firstRun = upcomingFirstRun(schedule.first_fire_at);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border px-3.5 pb-0.5 pt-3.5">
      <div className="flex items-start justify-between">
        <div className="flex flex-col gap-0.5">
          {schedule.name && (
            <span className="text-sm font-medium text-foreground">
              {schedule.name}
            </span>
          )}
          <span
            className={cn(
              "text-sm",
              schedule.name ? "text-muted-foreground" : "text-foreground",
            )}
          >
            {humanReadable}
          </span>
          <span className="font-mono text-xs text-muted-foreground">
            {schedule.workflow_schedule_id}
          </span>
        </div>
      </div>
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">
          {schedule.timezone}
        </span>
        <div className="flex items-center gap-2">
          <Switch
            checked={schedule.enabled}
            disabled={isToggling}
            onCheckedChange={(checked) =>
              onToggle(schedule.workflow_schedule_id, checked)
            }
          />
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            onClick={() => onDelete(schedule.workflow_schedule_id)}
          >
            <TrashIcon className="size-4 text-destructive" />
          </Button>
        </div>
      </div>
      {firstRun && (
        <div className="text-xs text-muted-foreground dark:text-slate-500">
          First run: {formatNextRun(firstRun, schedule.timezone)}
        </div>
      )}
      {nextRun && (
        <div className="text-xs text-muted-foreground dark:text-slate-500">
          {schedule.enabled ? "Next" : "Next (paused)"}:{" "}
          {formatNextRun(nextRun, schedule.timezone)}
        </div>
      )}
    </div>
  );
}

export { ScheduleCard };
