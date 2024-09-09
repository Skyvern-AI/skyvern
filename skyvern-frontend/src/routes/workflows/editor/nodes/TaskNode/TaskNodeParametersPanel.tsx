import { Checkbox } from "@/components/ui/checkbox";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";

type Props = {
  parameters: Array<string>;
  onParametersChange: (parameters: Array<string>) => void;
};

function TaskNodeParametersPanel({ parameters, onParametersChange }: Props) {
  const [workflowParameters] = useWorkflowParametersState();

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1>Parameters</h1>
        <span className="text-xs text-slate-300">
          Check off the parameters you want to use in this task.
        </span>
      </header>
      <div className="space-y-2">
        {workflowParameters.map((workflowParameter) => {
          return (
            <div
              key={workflowParameter.key}
              className="flex items-center gap-2 rounded-sm bg-slate-elevation1 px-3 py-2"
            >
              <Checkbox
                checked={parameters.includes(workflowParameter.key)}
                onCheckedChange={(checked) => {
                  if (checked) {
                    onParametersChange([...parameters, workflowParameter.key]);
                  } else {
                    onParametersChange(
                      parameters.filter(
                        (parameter) => parameter !== workflowParameter.key,
                      ),
                    );
                  }
                }}
              />
              <span className="text-xs">{workflowParameter.key}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { TaskNodeParametersPanel };
