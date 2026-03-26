import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { WorkflowParameterValueType } from "../types/workflowTypes";
import { PlusIcon } from "@radix-ui/react-icons";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import { useState } from "react";

type Props = {
  value: string;
  onChange: (value: string) => void;
  vault_type?: string;
};

function CredentialParameterSourceSelector({
  value,
  onChange,
  vault_type,
}: Props) {
  const { data: credentials, isLoading } = useCredentialsQuery({
    page_size: 100, // Reasonable limit for dropdown selector
    vault_type,
  });
  // Use local state for modal to avoid conflicts with other CredentialsModal instances
  const [isModalOpen, setIsModalOpen] = useState(false);
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const workflowParametersOfTypeCredentialId = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.dataType === WorkflowParameterValueType.CredentialId,
  );

  if (isLoading) {
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
            setIsModalOpen(true);
            return;
          }
          onChange(value);
        }}
      >
        <SelectTrigger>
          <SelectValue placeholder="Select a credential" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="new">
            <div className="flex items-center gap-2">
              <PlusIcon className="size-4" />
              <span>Add new credential</span>
            </div>
          </SelectItem>
          {options.length > 0 && <SelectSeparator />}
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <CredentialsModal
        isOpen={isModalOpen}
        onOpenChange={setIsModalOpen}
        onCredentialCreated={(id) => {
          onChange(id);
          setIsModalOpen(false);
        }}
      />
    </>
  );
}

export { CredentialParameterSourceSelector };
