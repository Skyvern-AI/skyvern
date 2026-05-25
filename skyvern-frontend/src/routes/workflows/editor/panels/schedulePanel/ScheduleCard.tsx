import { TrashIcon } from "@radix-ui/react-icons";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import type { WorkflowSchedule } from "@/routes/workflows/types/scheduleTypes";
import { cronToHumanReadable, formatNextRun, getNextRuns } from "./cronUtils";
import { cn } from "@/util/utils";

type Props = {
  schedule: WorkflowSchedule;
  isToggling?: boolean;
  onToggle: (scheduleId: string, enabled: boolean) => void;
  onDelete: (scheduleId: string) => void;
};

function ScheduleCard({ schedule, isToggling, onToggle, onDelete }: Props) {
  const humanReadable = cronToHumanReadable(schedule.cron_expression);
  const nextRuns = getNextRuns(schedule.cron_expression, schedule.timezone, 1);
  const nextRun = nextRuns[0];

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
      {nextRun && (
        <div className="text-xs text-muted-foreground">
          Next: {formatNextRun(nextRun, schedule.timezone)}
        </div>
      )}
    </div>
  );
}

export { ScheduleCard };
