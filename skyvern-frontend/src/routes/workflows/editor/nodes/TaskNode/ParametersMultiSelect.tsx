import { MultiSelect } from "@/components/ui/multi-select";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { HelpTooltip } from "@/components/HelpTooltip";
import { helpTooltips } from "../../helpContent";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { useContext, useMemo } from "react";
import CloudContext from "@/store/CloudContext";
import { parameterIsSkyvernCredential } from "../../types";

type Props = {
  availableOutputParameters: Array<string>;
  parameters: Array<string>;
  onParametersChange: (parameters: Array<string>) => void;
};

function ParametersMultiSelect({
  availableOutputParameters,
  parameters,
  onParametersChange,
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

  return (
    <div className="space-y-2">
      <header className="flex gap-2">
        <h1 className="text-xs text-slate-300">Parameters</h1>
        <HelpTooltip content={helpTooltips["task"]["parameters"]} />
      </header>
      <MultiSelect
        value={parameters}
        onValueChange={onParametersChange}
        options={options}
        maxCount={50}
      />
    </div>
  );
}

export { ParametersMultiSelect };
