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
import { ExclamationTriangleIcon, PlusIcon, CheckCircledIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "@/routes/credentials/useCredentialModalState";
import { useNodes } from "@xyflow/react";
import { AppNode } from "..";
import { isLoginNode } from "./types";
import {
  parameterIsSkyvernCredential,
  parameterIsBitwardenCredential,
  parameterIsOnePasswordCredential,
  parameterIsAzureVaultCredential,
} from "../../types";

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
  const isCloud = useContext(CloudContext);
  const { data: credentials = [], isFetching } = useCredentialsQuery({
    enabled: isCloud,
    page_size: 100,
  });

  // Get the set of credential IDs that are in the vault
  const credentialIdsInVault = useMemo(
    () => new Set(credentials.map((c) => c.credential_id)),
    [credentials],
  );

  // Determine which credential is currently selected (by credential_id)
  // This handles multiple cases:
  // 1. Skyvern credential parameters (have credentialId)
  // 2. Workflow input parameters with credential_id type and default value
  const selectedCredentialId = useMemo(() => {
    if (!value) return undefined;

    // Check if it's a credential parameter
    const credentialParam = credentialParameters.find((p) => p.key === value);
    if (credentialParam && parameterIsSkyvernCredential(credentialParam)) {
      return credentialParam.credentialId;
    }

    // Check if it's a workflow input parameter with credential_id type and default value
    const workflowParam = workflowParameters.find(
      (p) =>
        p.parameterType === "workflow" &&
        p.key === value &&
        p.dataType === "credential_id" &&
        typeof p.defaultValue === "string" &&
        p.defaultValue,
    );
    if (workflowParam && workflowParam.parameterType === "workflow") {
      return workflowParam.defaultValue as string;
    }

    return undefined;
  }, [value, credentialParameters, workflowParameters]);

  // Check if the selected credential is missing (deleted)
  const isCredentialMissing = useMemo(() => {
    if (!selectedCredentialId) return false;
    return !credentialIdsInVault.has(selectedCredentialId);
  }, [selectedCredentialId, credentialIdsInVault]);

  if (isCloud && isFetching) {
    return <Skeleton className="h-8 w-full" />;
  }

  const credentialOptions = credentials.map((credential) => ({
    label: credential.name,
    value: credential.credential_id,
    type: "credential" as const,
    hasBrowserProfile: !!credential.browser_profile_id,
    browserProfileUrl: credential.tested_url ?? null,
  }));

  // Only show non-Skyvern credential parameters (Bitwarden, 1Password, Azure Vault)
  // Skyvern credential parameters should never be shown - the actual credential is displayed directly
  const externalVaultParameterOptions = credentialParameters
    .filter((parameter) => {
      // Never show Skyvern credential parameters
      if (parameterIsSkyvernCredential(parameter)) {
        return false;
      }
      // Show Bitwarden, 1Password, Azure Vault credential parameters
      return (
        parameterIsBitwardenCredential(parameter) ||
        parameterIsOnePasswordCredential(parameter) ||
        parameterIsAzureVaultCredential(parameter)
      );
    })
    .map((parameter) => ({
      label: parameter.key,
      value: parameter.key,
      type: "parameter" as const,
    }));

  const options = [...credentialOptions, ...externalVaultParameterOptions];

  return (
    <>
      <Select
        key={value ?? "no-credential"}
        value={isCredentialMissing ? undefined : selectedCredentialId ?? value}
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
        <SelectTrigger
          className={
            isCredentialMissing
              ? "w-full border-red-500 text-red-500"
              : "w-full"
          }
        >
          {isCredentialMissing ? (
            <div className="flex items-center gap-2 text-red-500">
              <ExclamationTriangleIcon className="size-4" />
              <span>Credential not found</span>
            </div>
          ) : (
            <SelectValue placeholder="Select a credential" />
          )}
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              <div className="flex items-center gap-2">
                <span>{option.label}</span>
                {"hasBrowserProfile" in option && option.hasBrowserProfile && (
                  <span title={option.browserProfileUrl ? `Login-free credentials enabled for ${option.browserProfileUrl}` : "Login-free credentials enabled"}>
                    <CheckCircledIcon className="size-3 text-green-400" />
                  </span>
                )}
              </div>
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
