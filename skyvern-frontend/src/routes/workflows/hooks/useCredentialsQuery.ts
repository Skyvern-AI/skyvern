import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { useQuery } from "@tanstack/react-query";

type QueryReturnType = Array<CredentialApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = UseQueryOptions & {
  page?: number;
  page_size?: number;
  vault_type?: string;
  credential_type?: "password" | "credit_card" | "secret";
  search?: string;
};

function useCredentialsQuery(props: Props = {}) {
  const {
    page = 1,
    page_size = 25,
    vault_type,
    credential_type,
    search,
    ...queryOptions
  } = props;
  const credentialGetter = useCredentialGetter();
  // Read-only version-comparison canvases never need live credential data; suppress the fetch there for every caller at once.
  const scopeReadOnly = useWorkflowScopeReadOnly();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: [
      "credentials",
      page,
      page_size,
      vault_type,
      credential_type,
      search,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("page", String(page));
      params.set("page_size", String(page_size));
      if (vault_type) {
        params.set("vault_type", vault_type);
      }
      if (credential_type) {
        params.set("credential_type", credential_type);
      }
      if (search) {
        params.set("search", search);
      }
      return client.get("/credentials", { params }).then((res) => res.data);
    },
    refetchOnMount: "always",
    ...queryOptions,
    enabled: queryOptions.enabled !== false && !scopeReadOnly,
  });
}

export { useCredentialsQuery };
