import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";
import { useWorkflowParametersState } from "../editor/useWorkflowParametersState";
import { WorkflowParameterValueType } from "../types/workflowTypes";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

function CredentialParameterSourceSelector({ value, onChange }: Props) {
  const { data: credentials, isFetching } = useCredentialsQuery();
  const [workflowParameters] = useWorkflowParametersState();
  const workflowParametersOfTypeCredentialId = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.dataType === WorkflowParameterValueType.CredentialId,
  );

  if (isFetching) {
    return <Skeleton className="h-10 w-full" />;
  }

  if (!credentials) {
    return null;
  }

  const credentialOptions = credentials?.map((credential) => ({
    label: credential.name,
    value: credential.credential_id,
    type: "credential",
  }));

  const workflowParameterOptionsOfTypeCredentialId =
    workflowParametersOfTypeCredentialId.map((parameter) => ({
      label: parameter.key,
      value: parameter.key,
      type: "parameter",
    }));

  const options = [
    ...credentialOptions,
    ...workflowParameterOptionsOfTypeCredentialId,
  ];

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select a credential" />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { CredentialParameterSourceSelector };
