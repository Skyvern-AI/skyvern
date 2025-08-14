import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { WorkflowParameterValueType } from "../types/workflowTypes";
import { PlusIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "@/routes/credentials/useCredentialModalState";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

function CredentialParameterSourceSelector({ value, onChange }: Props) {
  const { data: credentials, isFetching } = useCredentialsQuery();
  const { setIsOpen, setType } = useCredentialModalState();
  const { parameters: workflowParameters } = useWorkflowParametersStore();
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
    <>
      <Select
        value={value}
        onValueChange={(value) => {
          if (value === "new") {
            setIsOpen(true);
            setType(CredentialModalTypes.PASSWORD);
            return;
          }
          onChange(value);
        }}
      >
        <SelectTrigger>
          <SelectValue placeholder="Select a credential" />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
          <SelectItem value="new">
            <div className="flex items-center gap-2">
              <PlusIcon className="size-4" />
              <span>Add new credential</span>
            </div>
          </SelectItem>
        </SelectContent>
      </Select>
      <CredentialsModal
        onCredentialCreated={(id) => {
          onChange(id);
          setIsOpen(false);
        }}
      />
    </>
  );
}

export { CredentialParameterSourceSelector };
