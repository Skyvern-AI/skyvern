import { useCallback } from "react";

import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useClientIdStore } from "@/store/useClientIdStore";
import { getCredentialParam } from "@/util/env";

export function useWebSocketParams(): () => Promise<string> {
  const credentialGetter = useCredentialGetter();
  const clientId = useClientIdStore((state) => state.clientId);

  return useCallback(async () => {
    const params = new URLSearchParams(
      await getCredentialParam(credentialGetter),
    );
    params.set("client_id", clientId);
    return params.toString();
  }, [clientId, credentialGetter]);
}
