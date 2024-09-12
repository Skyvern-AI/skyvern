import { Checkbox } from "@/components/ui/checkbox";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";

type Props = {
  availableOutputParameters: Array<string>;
  parameters: Array<string>;
  onParametersChange: (parameters: Array<string>) => void;
};

function TaskNodeParametersPanel({
  availableOutputParameters,
  parameters,
  onParametersChange,
}: Props) {
  const [workflowParameters] = useWorkflowParametersState();
  const keys = workflowParameters
    .map((parameter) => parameter.key)
    .concat(availableOutputParameters);
  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1>Parameters</h1>
        <span className="text-xs text-slate-300">
          Check off the parameters you want to use in this task.
        </span>
      </header>
      <div className="space-y-2">
        {keys.map((key) => {
          return (
            <div
              key={key}
              className="flex items-center gap-2 rounded-sm bg-slate-elevation1 px-3 py-2"
            >
              <Checkbox
                checked={parameters.includes(key)}
                onCheckedChange={(checked) => {
                  if (checked) {
                    onParametersChange([...parameters, key]);
                  } else {
                    onParametersChange(
                      parameters.filter((parameterKey) => parameterKey !== key),
                    );
                  }
                }}
              />
              <span className="text-xs">{key}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { TaskNodeParametersPanel };
