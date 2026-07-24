import { useNodes } from "@xyflow/react";
import { useMemo } from "react";

import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import { type AppNode } from "../editor/nodes";
import { isLoginNode } from "../editor/nodes/LoginNode/types";
import { parameterIsSkyvernCredential } from "../editor/types";
import { useCredentialQuery } from "./useCredentialQuery";
import {
  type CredScenario,
  deriveCredScenario,
} from "../components/browserProfileControlModel";

/**
 * Resolves the login-block credential context that decides the Auto-caption
 * variant (v26): no login block / credential with a saved profile / first use /
 * rotation.
 */
export function useBrowserProfileScenario(): {
  scn: CredScenario;
  credName?: string;
  credentialCount: number;
  credentialPinned: boolean;
} {
  const nodes = useNodes<AppNode>();
  const parameters = useWorkflowParametersStore((state) => state.parameters);
  const loginNode = useMemo(() => nodes.find(isLoginNode), [nodes]);
  const credentialParameterKey = loginNode?.data.parameterKeys?.[0];
  const credentialParam = useMemo(() => {
    if (!credentialParameterKey) return undefined;
    const param = parameters.find((p) => p.key === credentialParameterKey);
    if (!param || param.parameterType !== "credential") return undefined;
    return parameterIsSkyvernCredential(param) ? param : undefined;
  }, [parameters, credentialParameterKey]);
  // External vault credentials (Bitwarden/1Password/Azure) have no Skyvern
  // browser profile to save or reuse, so the credential-profile captions don't
  // apply — treat that login like no login block at all.
  const externalCredential = useMemo(() => {
    if (!credentialParameterKey) return false;
    const param = parameters.find((p) => p.key === credentialParameterKey);
    return (
      param?.parameterType === "credential" &&
      !parameterIsSkyvernCredential(param)
    );
  }, [parameters, credentialParameterKey]);
  const credentialIds = credentialParam?.credentialIds ?? [];
  const isRotating = credentialIds.length >= 2;
  const singleCredentialId = isRotating
    ? undefined
    : (credentialParam?.credentialId ?? undefined);
  const { data: credential, isFetched } = useCredentialQuery(
    singleCredentialId,
    { enabled: Boolean(singleCredentialId) },
  );

  const scn = deriveCredScenario({
    hasLoginNode: Boolean(loginNode),
    externalCredential,
    isRotating,
    hasSingleCredential: Boolean(singleCredentialId),
    isFetched,
    credentialHasProfile: Boolean(credential?.browser_profile_id),
  });

  return {
    scn,
    credName: credential?.name,
    credentialCount: credentialIds.length,
    credentialPinned: Boolean(
      credential?.pin_saved_session_ip && credential?.proxy_session_id,
    ),
  };
}
