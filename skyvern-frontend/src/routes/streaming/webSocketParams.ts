import { useCallback } from "react";

import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useClientIdStore } from "@/store/useClientIdStore";
import { getWebSocketProtocols } from "@/util/env";

export type WebSocketConnectParams = {
  /** Query string without the leading `?`; carries `client_id` only, never a credential. */
  params: string;
  protocols: string[];
};

export function useWebSocketParams(): () => Promise<WebSocketConnectParams> {
  const credentialGetter = useCredentialGetter();
  const clientId = useClientIdStore((state) => state.clientId);

  return useCallback(async () => {
    const params = new URLSearchParams({ client_id: clientId });
    return {
      params: params.toString(),
      protocols: await getWebSocketProtocols(credentialGetter),
    };
  }, [clientId, credentialGetter]);
}
