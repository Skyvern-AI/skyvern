import { useContext, useMemo } from "react";
import CloudContext from "@/store/CloudContext";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { parameterIsSkyvernCredential } from "../types";

/**
 * Resolves the totp_identifier stored on the credential a block currently
 * references (by parameter key). Returns null when no Skyvern credential is
 * selected or the credential has no stored totp_identifier. Used only to render
 * helper text — the runtime fallback to the credential's value lives in the
 * backend, so the value is never written into the block.
 *
 * Callers pass the first credential parameter key on the block; blocks carry at
 * most one credential parameter in practice, so subsequent keys are ignored.
 */
export function useSelectedCredentialTotpIdentifier(
  parameterKey: string | undefined,
): string | null {
  const isCloud = useContext(CloudContext);
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const { data: credentials = [] } = useCredentialsQuery({
    enabled: isCloud,
    page_size: 100,
  });

  return useMemo(() => {
    if (!parameterKey) {
      return null;
    }

    let credentialId: string | undefined;
    const credentialParam = workflowParameters
      .filter((p) => p.parameterType === "credential")
      .find((p) => p.key === parameterKey);
    if (credentialParam && parameterIsSkyvernCredential(credentialParam)) {
      credentialId = credentialParam.credentialId;
    } else {
      const workflowParam = workflowParameters.find(
        (p) =>
          p.parameterType === "workflow" &&
          p.key === parameterKey &&
          p.dataType === "credential_id" &&
          typeof p.defaultValue === "string" &&
          p.defaultValue,
      );
      if (workflowParam && workflowParam.parameterType === "workflow") {
        credentialId = workflowParam.defaultValue as string;
      }
    }

    if (!credentialId) {
      return null;
    }

    const credential = credentials.find(
      (c) => c.credential_id === credentialId,
    );
    if (
      credential &&
      credential.credential_type === "password" &&
      "totp_identifier" in credential.credential
    ) {
      return credential.credential.totp_identifier ?? null;
    }
    return null;
  }, [parameterKey, workflowParameters, credentials]);
}
