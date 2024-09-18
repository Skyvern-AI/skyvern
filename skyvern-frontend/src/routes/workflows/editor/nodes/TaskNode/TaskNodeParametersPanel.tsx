import { MultiSelect } from "@/components/ui/multi-select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
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

  const options = keys.map((key) => {
    return {
      label: key,
      value: key,
    };
  });

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
      <MultiSelect
        defaultValue={parameters}
        value={parameters}
        onValueChange={onParametersChange}
        options={options}
        maxCount={50}
      />
    </div>
  );
}

export { TaskNodeParametersPanel };
