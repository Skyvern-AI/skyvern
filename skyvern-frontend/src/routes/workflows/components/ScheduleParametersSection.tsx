import { Label } from "@/components/ui/label";
import { WorkflowParameterInput } from "@/routes/workflows/WorkflowParameterInput";
import type { Parameter } from "@/routes/workflows/types/workflowTypes";
import { getLabelForWorkflowParameterType } from "@/routes/workflows/editor/workflowEditorUtils";
import {
  hasUserFacingParameters,
  isRequired,
  isScheduleParameter,
} from "./scheduleParameters";

type Props = {
  parameters: ReadonlyArray<Parameter>;
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  errors?: Record<string, string | undefined>;
  disabled?: boolean;
};

function ScheduleParametersSection({
  parameters,
  values,
  onChange,
  errors,
  disabled,
}: Readonly<Props>) {
  if (!hasUserFacingParameters(parameters)) {
    return null;
  }

  const workflowParameters = parameters.filter(isScheduleParameter);

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <Label>Workflow Parameters</Label>
        <p className="text-xs text-slate-500">
          Values supplied here are used every time this schedule runs.
        </p>
      </div>
      <div className="space-y-4 rounded-md border border-slate-700 bg-slate-elevation3 p-3">
        {workflowParameters.map((parameter) => {
          const error = errors?.[parameter.key];
          const required = isRequired(parameter);
          return (
            <div key={parameter.key} className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label className="text-xs">
                  {parameter.key}
                  {required && (
                    <span
                      aria-label="required"
                      className="ml-1 text-destructive"
                    >
                      *
                    </span>
                  )}
                </Label>
                <span className="text-[10px] uppercase tracking-wide text-slate-500">
                  {getLabelForWorkflowParameterType(
                    parameter.workflow_parameter_type,
                  )}
                </span>
              </div>
              {parameter.description && (
                <p className="text-xs text-slate-500">
                  {parameter.description}
                </p>
              )}
              <fieldset disabled={disabled} className="contents">
                <WorkflowParameterInput
                  type={parameter.workflow_parameter_type}
                  value={values[parameter.key] ?? null}
                  required={required}
                  disabled={disabled}
                  onChange={(next) => onChange(parameter.key, next)}
                />
              </fieldset>
              {error && (
                <p className="text-xs text-destructive" role="alert">
                  {error}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { ScheduleParametersSection };
