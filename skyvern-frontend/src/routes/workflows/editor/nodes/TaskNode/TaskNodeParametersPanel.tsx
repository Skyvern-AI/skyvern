import { MultiSelect } from "@/components/ui/multi-select";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { HelpTooltip } from "@/components/HelpTooltip";
import { helpTooltips } from "../../helpContent";

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
  const { parameters: workflowParameters } = useWorkflowParametersStore();
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
    <div className="space-y-2">
      <header className="flex gap-2">
        <h1 className="text-xs text-slate-300">Parameters</h1>
        <HelpTooltip content={helpTooltips["task"]["parameters"]} />
      </header>
      <MultiSelect
        value={parameters}
        onValueChange={onParametersChange}
        options={options}
        maxCount={50}
      />
    </div>
  );
}

export { TaskNodeParametersPanel };
