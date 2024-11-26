import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";
import { useId } from "react";

type Props = {
  value?: string;
  onChange?: (value: string) => void;
};

function CredentialParameterSelector({ value, onChange }: Props) {
  const [workflowParameters] = useWorkflowParametersState();
  const credentialParameters = workflowParameters.filter(
    (parameter) => parameter.parameterType === "credential",
  );
  const noneItemValue = useId();

  return (
    <Select
      value={value}
      onValueChange={(value) => {
        if (value === noneItemValue) {
          onChange?.("");
        } else {
          onChange?.(value);
        }
      }}
    >
      <SelectTrigger className="w-full">
        <SelectValue placeholder="Select a credential parameter" />
      </SelectTrigger>
      <SelectContent>
        {credentialParameters.map((parameter) => (
          <SelectItem key={parameter.key} value={parameter.key}>
            {parameter.key}
          </SelectItem>
        ))}
        {credentialParameters.length === 0 && (
          <SelectItem value={noneItemValue} key={noneItemValue}>
            No credential parameters found
          </SelectItem>
        )}
      </SelectContent>
    </Select>
  );
}

export { CredentialParameterSelector };
