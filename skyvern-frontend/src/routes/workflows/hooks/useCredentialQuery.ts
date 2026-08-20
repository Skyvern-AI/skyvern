import { getClient } from "@/api/AxiosClient";
import type { CredentialApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";

type UseQueryOptions = Omit<
  Parameters<typeof useQuery<CredentialApiResponse>>[0],
  "queryKey" | "queryFn"
>;

function useCredentialQuery(
  credentialId: string | undefined,
  options: UseQueryOptions = {},
) {
  const credentialGetter = useCredentialGetter();
  const scopeReadOnly = useWorkflowScopeReadOnly();

  return useQuery<CredentialApiResponse>({
    queryKey: ["credentials", "detail", credentialId],
    queryFn: async () => {
      if (!credentialId) {
        throw new Error("Credential ID is required");
      }
      const client = await getClient(credentialGetter);
      return client
        .get(`/credentials/${encodeURIComponent(credentialId)}`)
        .then((response) => response.data);
    },
    ...options,
    enabled:
      Boolean(credentialId) && options.enabled !== false && !scopeReadOnly,
  });
}

function isCredentialNotFoundError(error: unknown) {
  return isAxiosError(error) && error.response?.status === 404;
}

export { isCredentialNotFoundError, useCredentialQuery };
