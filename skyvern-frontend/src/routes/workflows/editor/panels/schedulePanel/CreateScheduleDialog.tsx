import { useEffect, useState } from "react";
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
import { ScheduleConfigFields } from "@/routes/workflows/components/ScheduleConfigFields";
import { ScheduleParametersSection } from "@/routes/workflows/components/ScheduleParametersSection";
import { buildScheduleParametersPayload } from "@/routes/workflows/components/scheduleParameters";
import { useScheduleParameterState } from "@/routes/workflows/hooks/useScheduleParameterState";
import {
  getLocalTimezone,
  isValidCron,
  meetsMinCronInterval,
} from "./cronUtils";

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

  const valid = isValidCron(cronExpression);
  const cronAccepted = valid && meetsMinCronInterval(cronExpression);

  function resetFormState() {
    setCronExpression("0 9 * * *");
    setTimezone(getLocalTimezone());
    setName("");
    setDescription("");
    resetParameters();
  }

  function handleSubmit() {
    const parametersValid = validateParameters();
    if (!cronAccepted || !parametersValid) return;
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
        <Button size="sm" className="h-8 gap-1.5">
          <PlusIcon className="size-3" />
          Add
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Schedule</DialogTitle>
          <DialogDescription>
            Configure when this agent should run automatically.
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

          <ScheduleConfigFields
            cronExpression={cronExpression}
            timezone={timezone}
            onCronChange={setCronExpression}
            onTimezoneChange={setTimezone}
            disabled={isPending}
          />
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button disabled={!cronAccepted || isPending} onClick={handleSubmit}>
            {isPending ? "Creating..." : "Create Schedule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CreateScheduleDialog };
