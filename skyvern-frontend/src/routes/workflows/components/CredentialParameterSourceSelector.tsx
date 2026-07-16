import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { WorkflowParameterValueType } from "../types/workflowTypes";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import { useState } from "react";
import { CredentialCombobox } from "./CredentialCombobox";

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
  const [isModalOpen, setIsModalOpen] = useState(false);
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const workflowParametersOfTypeCredentialId = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.dataType === WorkflowParameterValueType.CredentialId,
  );

  const workflowParameterOptionsOfTypeCredentialId =
    workflowParametersOfTypeCredentialId.map((parameter) => ({
      label: parameter.key,
      value: parameter.key,
    }));
  const selectedParameter = workflowParameterOptionsOfTypeCredentialId.find(
    (option) => option.value === value,
  );

  return (
    <>
      <CredentialCombobox
        value={value}
        selectedCredentialId={
          selectedParameter ? undefined : value || undefined
        }
        onValueChange={(nextValue) => onChange(nextValue)}
        extraOptions={workflowParameterOptionsOfTypeCredentialId}
        onAddNew={() => setIsModalOpen(true)}
        query={{ vaultType: vault_type }}
      />
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
