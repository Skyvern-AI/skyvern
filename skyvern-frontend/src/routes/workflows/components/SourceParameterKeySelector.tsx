import { useNodes } from "@xyflow/react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode, isWorkflowBlockNode } from "../editor/nodes";
import { getOutputParameterKey } from "../editor/workflowEditorUtils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Props = {
  value?: string;
  onChange: (value: string) => void;
};

function SourceParameterKeySelector({ value, onChange }: Props) {
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const nodes = useNodes<AppNode>();
  const contextParameterKeys = workflowParameters
    .filter((parameter) => parameter.parameterType !== "credential")
    .map((parameter) => parameter.key);
  const outputParameterKeys = nodes
    .filter(isWorkflowBlockNode)
    .map((node) => getOutputParameterKey(node.data.label));

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select a parameter" />
      </SelectTrigger>
      <SelectContent>
        {[...contextParameterKeys, ...outputParameterKeys].map(
          (parameterKey) => (
            <SelectItem key={parameterKey} value={parameterKey}>
              {parameterKey}
            </SelectItem>
          ),
        )}
      </SelectContent>
    </Select>
  );
}

export { SourceParameterKeySelector };
