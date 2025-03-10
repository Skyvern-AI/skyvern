import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import CloudContext from "@/store/CloudContext";
import { useContext, useId } from "react";
import { useWorkflowParametersState } from "../../useWorkflowParametersState";

type Props = {
  value?: string;
  onChange?: (value: string) => void;
};

function LoginBlockCredentialSelector({ value, onChange }: Props) {
  const [workflowParameters, setWorkflowParameters] =
    useWorkflowParametersState();
  const credentialParameters = workflowParameters.filter(
    (parameter) => parameter.parameterType === "credential",
  );
  const credentialInputParameters = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.dataType === "credential_id",
  );
  const isCloud = useContext(CloudContext);
  const { data: credentials = [], isLoading } = useCredentialsQuery({
    enabled: isCloud,
  });
  const noneItemValue = useId();

  if (isCloud && isLoading) {
    return <Skeleton className="h-8 w-full" />;
  }

  const credentialOptions = credentials.map((credential) => ({
    label: credential.name,
    value: credential.credential_id,
    type: "credential",
  }));

  const credentialParameterOptions = credentialParameters.map((parameter) => ({
    label: parameter.key,
    value: parameter.key,
    type: "parameter",
  }));

  const credentialInputParameterOptions = credentialInputParameters.map(
    (parameter) => ({
      label: parameter.key,
      value: parameter.key,
      type: "parameter",
    }),
  );

  const filteredCredentialParameterOptions = credentialParameterOptions.filter(
    (option) =>
      !credentialOptions.some(
        (credential) => credential.value === option.value,
      ),
  );

  const options = [
    ...credentialOptions,
    ...filteredCredentialParameterOptions,
    ...credentialInputParameterOptions,
  ];

  return (
    <Select
      value={value}
      onValueChange={(value) => {
        const option = options.find((option) => option.value === value);
        if (option?.type === "credential") {
          const existingCredential = workflowParameters.find((parameter) => {
            return (
              parameter.parameterType === "credential" &&
              "credentialId" in parameter &&
              parameter.credentialId === value &&
              parameter.key === value
            );
          });
          if (!existingCredential) {
            setWorkflowParameters((prev) => [
              ...prev,
              {
                parameterType: "credential",
                credentialId: value,
                key: value,
              },
            ]);
          }
        }
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
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
        {options.length === 0 && (
          <SelectItem value={noneItemValue} key={noneItemValue}>
            No credential parameters found
          </SelectItem>
        )}
      </SelectContent>
    </Select>
  );
}

export { LoginBlockCredentialSelector };
