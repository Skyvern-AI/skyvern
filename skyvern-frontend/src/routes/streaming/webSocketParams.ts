import { useCallback } from "react";
import { nanoid } from "nanoid";

import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getWebSocketProtocols } from "@/util/env";

export type WebSocketConnectParams = {
  /** Query string without the leading `?`; carries `client_id` only, never a credential. */
  params: string;
  protocols: string[];
};

/**
 * The server keys each live view's channels by client id, so every mounted stream needs its own;
 * the sockets one stream opens share it so take/cede control finds that stream's VNC channel.
 */
export function generateClientId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return nanoid();
  }
}

export function useWebSocketParams(
  clientId: string,
): () => Promise<WebSocketConnectParams> {
  const credentialGetter = useCredentialGetter();

  return useCallback(async () => {
    const params = new URLSearchParams({ client_id: clientId });
    return {
      params: params.toString(),
      protocols: await getWebSocketProtocols(credentialGetter),
    };
  }, [clientId, credentialGetter]);
}
