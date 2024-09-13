import { Checkbox } from "@/components/ui/checkbox";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";
import { Label } from "@/components/ui/label";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";

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

  function toggleParameter(key: string) {
    if (parameters.includes(key)) {
      onParametersChange(
        parameters.filter((parameterKey) => parameterKey !== key),
      );
    } else {
      onParametersChange([...parameters, key]);
    }
  }

  return (
    <div className="space-y-4">
      <header className="flex gap-2">
        <h1 className="text-xs text-slate-300">Parameters</h1>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger>
              <QuestionMarkCircledIcon className="h-4 w-4" />
            </TooltipTrigger>
            <TooltipContent>
              Select the parameters that will be passed to the task.
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </header>
      <div className="flex flex-wrap gap-2">
        {keys.map((key) => {
          return (
            <div
              key={key}
              className="flex cursor-pointer items-center gap-2 rounded-sm bg-slate-elevation1 px-3 py-2"
              id={key}
            >
              <Checkbox
                checked={parameters.includes(key)}
                onCheckedChange={() => {
                  toggleParameter(key);
                }}
              />
              <Label
                htmlFor={key}
                className="cursor-pointer text-xs"
                onClick={() => {
                  toggleParameter(key);
                }}
              >
                {key}
              </Label>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { TaskNodeParametersPanel };
