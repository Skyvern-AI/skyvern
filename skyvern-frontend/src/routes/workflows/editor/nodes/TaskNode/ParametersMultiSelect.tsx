import { MultiSelect } from "@/components/ui/multi-select";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { HelpTooltip } from "@/components/HelpTooltip";
import { helpTooltips } from "../../helpContent";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { useCallback, useContext, useMemo } from "react";
import CloudContext from "@/store/CloudContext";
import { parameterIsSkyvernCredential } from "../../types";

type Props = {
  availableOutputParameters: Array<string>;
  parameters: Array<string>;
  onParametersChange: (parameters: Array<string>) => void;
  /** Called when a credential parameter with a totp_identifier is added */
  onCredentialTotpIdentifier?: (totpIdentifier: string) => void;
};

function ParametersMultiSelect({
  availableOutputParameters,
  parameters,
  onParametersChange,
  onCredentialTotpIdentifier,
}: Props) {
  const isCloud = useContext(CloudContext);
  const { parameters: workflowParameters } = useWorkflowParametersStore();

  // Fetch credentials to check for orphaned Skyvern credential parameters
  const { data: credentials = [], isSuccess } = useCredentialsQuery({
    enabled: isCloud,
    page_size: 100,
  });

  // Get the set of credential IDs that exist in the vault
  const credentialIdsInVault = useMemo(
    () => new Set(credentials.map((c) => c.credential_id)),
    [credentials],
  );

  const keys = workflowParameters
    .map((parameter) => parameter.key)
    .concat(availableOutputParameters);

  // Build options with warning labels for orphaned Skyvern credential parameters
  const options = useMemo(() => {
    return keys.map((key) => {
      const param = workflowParameters.find((p) => p.key === key);

      // Check if this is an orphaned Skyvern credential parameter
      const isOrphanedCredential =
        isSuccess &&
        param &&
        param.parameterType === "credential" &&
        parameterIsSkyvernCredential(param) &&
        !credentialIdsInVault.has(param.credentialId);

      return {
        label: isOrphanedCredential ? `⚠️ ${key} (missing credential)` : key,
        value: key,
      };
    });
  }, [keys, workflowParameters, isSuccess, credentialIdsInVault]);

  const handleValueChange = useCallback(
    (newParameters: Array<string>) => {
      onParametersChange(newParameters);

      // Check if a credential parameter was newly added
      if (onCredentialTotpIdentifier) {
        const addedKeys = newParameters.filter(
          (key) => !parameters.includes(key),
        );
        for (const key of addedKeys) {
          const param = workflowParameters.find((p) => p.key === key);
          if (
            param &&
            param.parameterType === "credential" &&
            parameterIsSkyvernCredential(param)
          ) {
            const credential = credentials.find(
              (c) => c.credential_id === param.credentialId,
            );
            if (
              credential &&
              credential.credential_type === "password" &&
              "totp_identifier" in credential.credential &&
              credential.credential.totp_identifier
            ) {
              onCredentialTotpIdentifier(credential.credential.totp_identifier);
              break;
            }
          }
        }
      }
    },
    [
      onParametersChange,
      onCredentialTotpIdentifier,
      parameters,
      workflowParameters,
      credentials,
    ],
  );

  return (
    <div className="space-y-2">
      <header className="flex gap-2">
        <h1 className="text-xs text-slate-300">Parameters</h1>
        <HelpTooltip content={helpTooltips["task"]["parameters"]} />
      </header>
      <MultiSelect
        value={parameters}
        onValueChange={handleValueChange}
        options={options}
        maxCount={50}
      />
    </div>
  );
}

export { ParametersMultiSelect };
