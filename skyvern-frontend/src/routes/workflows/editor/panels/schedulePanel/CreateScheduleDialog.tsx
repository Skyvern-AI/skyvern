import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PlusIcon } from "@radix-ui/react-icons";
import type { Parameter } from "@/routes/workflows/types/workflowTypes";
import { ScheduleParametersSection } from "@/routes/workflows/components/ScheduleParametersSection";
import { buildScheduleParametersPayload } from "@/routes/workflows/components/scheduleParameters";
import { useScheduleParameterState } from "@/routes/workflows/hooks/useScheduleParameterState";
import {
  CRON_PRESETS,
  cronToHumanReadable,
  isValidCron,
  getNextRuns,
  formatNextRun,
  getTimezones,
  getLocalTimezone,
} from "./cronUtils";
import { cn } from "@/util/utils";

type Props = {
  workflowParameters: ReadonlyArray<Parameter>;
  onSubmit: (
    cronExpression: string,
    timezone: string,
    name: string,
    description: string,
    parameters: Record<string, unknown> | null,
    callbacks: { onSuccess: () => void },
  ) => void;
  isPending?: boolean;
};

function CreateScheduleDialog({
  workflowParameters,
  onSubmit,
  isPending,
}: Readonly<Props>) {
  const [open, setOpen] = useState(false);
  const [cronExpression, setCronExpression] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState(getLocalTimezone);
  const [timezoneFilter, setTimezoneFilter] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const {
    values: parameters,
    errors: parameterErrors,
    handleChange: handleParameterChange,
    validate: validateParameters,
    reset: resetParameters,
  } = useScheduleParameterState(workflowParameters);

  // Re-seed parameter state if the workflow definition resolves after mount
  // (e.g., the query was still loading when this dialog mounted) or if the
  // workflow's parameters change while the dialog is closed. Also re-fires
  // when the workflowParameters reference changes so newly-arrived
  // definitions get seeded with their defaults. Skipped while the dialog is
  // open so we don't clobber user input mid-edit.
  useEffect(() => {
    if (!open) {
      resetParameters();
    }
  }, [open, workflowParameters, resetParameters]);

  const allTimezones = useMemo(() => getTimezones(), []);
  const filteredTimezones = useMemo(() => {
    if (timezoneFilter === null) return allTimezones;
    if (!timezoneFilter) return allTimezones;
    const lower = timezoneFilter.toLowerCase();
    return allTimezones.filter((tz) => tz.toLowerCase().includes(lower));
  }, [allTimezones, timezoneFilter]);

  const valid = isValidCron(cronExpression);
  const humanReadable = valid ? cronToHumanReadable(cronExpression) : null;
  const nextRuns = valid ? getNextRuns(cronExpression, timezone, 5) : [];

  function resetFormState() {
    setCronExpression("0 9 * * *");
    setTimezone(getLocalTimezone());
    setTimezoneFilter(null);
    setName("");
    setDescription("");
    resetParameters();
  }

  function handleSubmit() {
    const parametersValid = validateParameters();
    if (!valid || !parametersValid) return;
    const payload = buildScheduleParametersPayload(
      parameters,
      workflowParameters,
    );
    onSubmit(cronExpression, timezone, name, description, payload, {
      onSuccess: () => {
        setOpen(false);
        resetFormState();
      },
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) resetFormState();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 gap-1.5">
          <PlusIcon className="size-3" />
          Add
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Schedule</DialogTitle>
          <DialogDescription>
            Configure when this workflow should run automatically.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-4">
          <div className="space-y-2">
            <Label>Name (optional)</Label>
            <Input
              placeholder="Auto-generated if empty"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Description (optional)</Label>
            <Input
              placeholder="Add a description..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>

          <ScheduleParametersSection
            parameters={workflowParameters}
            values={parameters}
            onChange={handleParameterChange}
            errors={parameterErrors}
            disabled={isPending}
          />

          <div className="space-y-2">
            <Label>Quick Presets</Label>
            <div className="flex flex-wrap gap-2">
              {CRON_PRESETS.map((preset) => (
                <Button
                  key={preset.label}
                  variant={
                    cronExpression === preset.expression
                      ? "default"
                      : "secondary"
                  }
                  size="sm"
                  onClick={() => setCronExpression(preset.expression)}
                >
                  {preset.label}
                </Button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <Label>Cron Expression</Label>
            <Input
              value={cronExpression}
              onChange={(e) => setCronExpression(e.target.value)}
              placeholder="* * * * *"
              className={cn(!valid && cronExpression && "border-destructive")}
            />
            {humanReadable && (
              <p className="text-sm text-slate-400">{humanReadable}</p>
            )}
            {!valid && cronExpression && (
              <p className="text-sm text-destructive">
                Invalid cron expression
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label>Timezone</Label>
            <Input
              value={timezoneFilter ?? timezone}
              onChange={(e) => setTimezoneFilter(e.target.value)}
              onFocus={(e) => e.currentTarget.select()}
              onBlur={() => {
                if (
                  filteredTimezones.length === 1 &&
                  filteredTimezones[0] !== undefined
                ) {
                  setTimezone(filteredTimezones[0]);
                }
                setTimezoneFilter(null);
              }}
              placeholder="Search timezones..."
            />
            {timezoneFilter !== null && (
              <div className="max-h-40 overflow-y-auto rounded-md border border-slate-700 bg-slate-elevation3">
                {filteredTimezones.slice(0, 20).map((tz) => (
                  <button
                    key={tz}
                    type="button"
                    className={cn(
                      "w-full px-3 py-1.5 text-left text-sm hover:bg-slate-700",
                      tz === timezone && "bg-slate-700 text-slate-50",
                    )}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      setTimezone(tz);
                      setTimezoneFilter(null);
                    }}
                  >
                    {tz}
                  </button>
                ))}
                {filteredTimezones.length === 0 && (
                  <div className="px-3 py-2 text-sm text-slate-500">
                    No timezones found
                  </div>
                )}
              </div>
            )}
            <p className="text-xs text-slate-500">Current: {timezone}</p>
          </div>

          {nextRuns.length > 0 && (
            <div className="space-y-2">
              <Label>Next Scheduled Runs</Label>
              <div className="space-y-1 rounded-md border border-slate-700 bg-slate-elevation3 p-3">
                {nextRuns.map((run) => (
                  <div
                    key={run.toISOString()}
                    className="text-xs text-slate-400"
                  >
                    {formatNextRun(run, timezone)}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button disabled={!valid || isPending} onClick={handleSubmit}>
            {isPending ? "Creating..." : "Create Schedule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CreateScheduleDialog };
