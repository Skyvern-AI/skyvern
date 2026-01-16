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
import { useContext, useMemo } from "react";
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
    page_size: 100,
  });

  // Determine which credential is currently selected (by credential_id)
  // This must be before the early return to comply with React hooks rules
  const selectedCredentialId = useMemo(() => {
    if (!value) return undefined;
    const parameter = credentialParameters.find((p) => p.key === value);
    if (parameter && parameterIsSkyvernCredential(parameter)) {
      return parameter.credentialId;
    }
    return undefined;
  }, [value, credentialParameters]);

  if (isCloud && isFetching) {
    return <Skeleton className="h-8 w-full" />;
  }

  const credentialOptions = credentials.map((credential) => ({
    label: credential.name,
    value: credential.credential_id,
    type: "credential",
  }));

  // Get the set of credential IDs that are in the vault
  const credentialIdsInVault = new Set(credentials.map((c) => c.credential_id));

  // Filter credential parameters to only show those that reference credentials
  // NOT in the vault (e.g., Bitwarden, 1Password, Azure Vault credentials)
  // Skyvern credential parameters are excluded because the actual credential is already shown
  const filteredCredentialParameterOptions = credentialParameters
    .filter((parameter) => {
      if (parameterIsSkyvernCredential(parameter)) {
        // Don't show Skyvern credential parameters if the credential is in the vault
        return !credentialIdsInVault.has(parameter.credentialId);
      }
      // Show non-Skyvern credential parameters (Bitwarden, 1Password, etc.)
      return true;
    })
    .map((parameter) => ({
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

  const options = [
    ...credentialOptions,
    ...filteredCredentialParameterOptions,
    ...credentialInputParameterOptions,
  ];

  return (
    <>
      <Select
        value={selectedCredentialId ?? value}
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

          // Check if current value references a Skyvern credential
          const currentParameter = workflowParameters.find((parameter) => {
            if (parameter.parameterType !== "credential") return false;
            if (!parameterIsSkyvernCredential(parameter)) return false;
            return parameter.key === value;
          });

          const isUsedInOtherLoginNodes =
            value &&
            loginNodes.some((node) => node.data.parameterKeys.includes(value));

          // Only delete old parameter if it's not used elsewhere
          const deleteOldParameter =
            currentParameter && !isUsedInOtherLoginNodes;

          if (deleteOldParameter) {
            newParameters = newParameters.filter(
              (parameter) => parameter.key !== value,
            );
          }

          // Check if user selected an actual credential (by credential_id)
          const selectedCredential = credentialOptions.find(
            (option) => option.value === newValue,
          );

          let parameterKeyToUse = newValue;

          if (selectedCredential) {
            // User selected an actual credential
            const existingParameter = newParameters.find((parameter) => {
              return (
                parameter.parameterType === "credential" &&
                parameterIsSkyvernCredential(parameter) &&
                parameter.credentialId === newValue
              );
            });

            if (existingParameter) {
              // Reuse the existing parameter
              parameterKeyToUse = existingParameter.key;
            } else {
              // Create a new parameter for this credential
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
          }
          // If user selected a parameter (non-Skyvern credential or input parameter)
          // just use it directly (parameterKeyToUse is already set to newValue)

          // Update Zustand store first, then call onChange
          // This ensures workflowParameters is updated before the parent re-renders
          // with the new value, so selectedCredentialId computes correctly
          setWorkflowParameters(newParameters);
          onChange?.(parameterKeyToUse);
        }}
      >
        <SelectTrigger className="w-full">
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
          const existingKeys = workflowParameters.map((param) => param.key);
          const newKey = generateDefaultCredentialParameterKey(existingKeys);

          // Update Zustand store first, then call onChange
          setWorkflowParameters([
            ...workflowParameters,
            {
              parameterType: "credential",
              credentialId: id,
              key: newKey,
            },
          ]);
          onChange?.(newKey);
        }}
      />
    </>
  );
}

export { LoginBlockCredentialSelector };
