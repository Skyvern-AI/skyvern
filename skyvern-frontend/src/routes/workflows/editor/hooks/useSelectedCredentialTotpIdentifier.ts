import { useMemo } from "react";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { useCredentialQuery } from "@/routes/workflows/hooks/useCredentialQuery";
import { useSkyvernCredentialSourceAvailable } from "@/routes/workflows/hooks/useSkyvernCredentialSourceAvailable";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { parameterIsSkyvernCredential } from "../types";

/**
 * Resolves the totp_identifier(s) stored on the credential(s) a block
 * currently references (by parameter key). When the parameter is a rotating
 * Skyvern credential (2+ credentialIds), returns every distinct identifier
 * found, comma-joined. Returns null when no Skyvern credential is selected or
 * none of the resolved credentials have a stored totp_identifier. Used only to
 * render helper text — the runtime fallback to the credential's value lives in
 * the backend, so the value is never written into the block.
 *
 * Callers pass the first credential parameter key on the block; blocks carry at
 * most one credential parameter in practice, so subsequent keys are ignored.
 */
export function useSelectedCredentialTotpIdentifier(
  parameterKey: string | undefined,
): string | null {
  const skyvernCredentialSourceAvailable =
    useSkyvernCredentialSourceAvailable();
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const { data: credentials = [] } = useCredentialsQuery({
    enabled: skyvernCredentialSourceAvailable,
    page_size: 100,
  });

  const credentialIds = useMemo(() => {
    if (!parameterKey) {
      return [];
    }

    const credentialParam = workflowParameters
      .filter((p) => p.parameterType === "credential")
      .find((p) => p.key === parameterKey);
    if (credentialParam && parameterIsSkyvernCredential(credentialParam)) {
      const rotatedIds = credentialParam.credentialIds;
      return rotatedIds && rotatedIds.length > 0
        ? rotatedIds
        : [credentialParam.credentialId];
    }

    const workflowParam = workflowParameters.find(
      (p) =>
        p.parameterType === "workflow" &&
        p.key === parameterKey &&
        p.dataType === "credential_id" &&
        typeof p.defaultValue === "string" &&
        p.defaultValue,
    );
    if (workflowParam && workflowParam.parameterType === "workflow") {
      return [workflowParam.defaultValue as string];
    }
    return [];
  }, [parameterKey, workflowParameters]);

  const primaryCredentialId = credentialIds[0];
  const credentialFromList = credentials.find(
    (credential) => credential.credential_id === primaryCredentialId,
  );
  // ponytail: only the primary (first-rotated) id gets a detail-fetch fallback;
  // a fetch per rotated id would need a variable number of hooks. Other
  // rotated ids resolve from the already-fetched page (page_size 100).
  const credentialQuery = useCredentialQuery(primaryCredentialId, {
    enabled: skyvernCredentialSourceAvailable && !credentialFromList,
  });

  return useMemo(() => {
    const knownCredentials =
      credentialFromList || !credentialQuery.data
        ? credentials
        : [...credentials, credentialQuery.data];

    const identifiers = credentialIds
      .map((id) => knownCredentials.find((c) => c.credential_id === id))
      .map((credential) =>
        credential &&
        credential.credential_type === "password" &&
        "totp_identifier" in credential.credential
          ? (credential.credential.totp_identifier ?? null)
          : null,
      )
      .filter((identifier): identifier is string => Boolean(identifier));

    if (identifiers.length === 0) {
      return null;
    }
    return Array.from(new Set(identifiers)).join(", ");
  }, [credentialIds, credentialFromList, credentialQuery.data, credentials]);
}
