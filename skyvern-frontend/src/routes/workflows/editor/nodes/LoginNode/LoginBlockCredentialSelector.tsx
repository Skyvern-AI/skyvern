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
import { useContext } from "react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import { PlusIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "@/routes/credentials/useCredentialModalState";
import { useNodes } from "@xyflow/react";
import { AppNode } from "..";
import { isLoginNode } from "./types";
import { parameterIsSkyvernCredential } from "../../types";

type Props = {
  nodeId: string;
  value?: string;
  onChange?: (value: string) => void;
};

// Function to generate a unique credential parameter key
function generateDefaultCredentialParameterKey(existingKeys: string[]): string {
  const baseName = "credentials";

  // Check if "credentials" is available
  if (!existingKeys.includes(baseName)) {
    return baseName;
  }

  // Find the next available number
  let counter = 1;
  while (existingKeys.includes(`${baseName}_${counter}`)) {
    counter++;
  }

  return `${baseName}_${counter}`;
}

function LoginBlockCredentialSelector({ nodeId, value, onChange }: Props) {
  const { setIsOpen, setType } = useCredentialModalState();
  const nodes = useNodes<AppNode>();
  const {
    parameters: workflowParameters,
    setParameters: setWorkflowParameters,
  } = useWorkflowParametersStore();
  const credentialParameters = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "credential" ||
      parameter.parameterType === "onepassword",
  );
  const credentialInputParameters = workflowParameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.dataType === "credential_id",
  );
  const isCloud = useContext(CloudContext);
  const { data: credentials = [], isFetching } = useCredentialsQuery({
    enabled: isCloud,
  });

  if (isCloud && isFetching) {
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
    <>
      <Select
        value={value}
        onValueChange={(newValue) => {
          if (newValue === "new") {
            setIsOpen(true);
            setType(CredentialModalTypes.PASSWORD);
            return;
          }

          let newParameters = [...workflowParameters];

          const loginNodes = nodes
            .filter((node) => node.id !== nodeId)
            .filter(isLoginNode);

          const thereIsAParameterWithThisValue = workflowParameters.some(
            (parameter) =>
              parameter.parameterType === "credential" &&
              parameterIsSkyvernCredential(parameter) &&
              parameter.credentialId === value,
          );

          const isUsedInOtherLoginNodes =
            value &&
            loginNodes.some((node) => node.data.parameterKeys.includes(value));

          const deleteOldParameter =
            thereIsAParameterWithThisValue && !isUsedInOtherLoginNodes;

          if (deleteOldParameter) {
            newParameters = newParameters.filter(
              (parameter) => parameter.key !== value,
            );
          }

          const option = options.find((option) => option.value === newValue);
          let parameterKeyToUse = newValue;
          if (option?.type === "credential") {
            const existingCredential = workflowParameters.find((parameter) => {
              return (
                parameter.parameterType === "credential" &&
                "credentialId" in parameter &&
                parameter.credentialId === newValue
              );
            });
            if (existingCredential) {
              // Use the existing parameter's key
              parameterKeyToUse = existingCredential.key;
            } else {
              // Generate a new parameter key based on existing keys
              const existingKeys = newParameters.map((param) => param.key);
              const newKey =
                generateDefaultCredentialParameterKey(existingKeys);
              parameterKeyToUse = newKey;

              newParameters = [
                ...newParameters,
                {
                  parameterType: "credential",
                  credentialId: newValue,
                  key: newKey,
                },
              ];
            }
          } else if (deleteOldParameter) {
            newParameters = newParameters.filter(
              (parameter) => parameter.key !== value,
            );
          }
          onChange?.(parameterKeyToUse);
          setWorkflowParameters(newParameters);
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
          const existingKeys = workflowParameters.map((param) => param.key);
          const newKey = generateDefaultCredentialParameterKey(existingKeys);

          onChange?.(newKey);
          setWorkflowParameters([
            ...workflowParameters,
            {
              parameterType: "credential",
              credentialId: id,
              key: newKey,
            },
          ]);
        }}
      />
    </>
  );
}

export { LoginBlockCredentialSelector };
