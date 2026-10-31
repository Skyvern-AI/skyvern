import { queryOptions, useQuery } from "@tanstack/react-query";
import { getClient, type CredentialGetter } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  type ActiveOrgId,
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

export type IntegrationCatalogEntry = {
  id: string;
  status: "live" | "comingSoon";
};

const INTEGRATION_CATALOG_QUERY_KEY = ["integrationCatalog"] as const;

export function integrationCatalogQueryOptions<
  T extends IntegrationCatalogEntry,
>(credentialGetter: CredentialGetter | null, activeOrgId: ActiveOrgId) {
  return queryOptions<T[]>({
    queryKey: getOrgScopedQueryKey(
      INTEGRATION_CATALOG_QUERY_KEY,
      getActiveOrgQueryKeyScope(activeOrgId),
    ),
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<T[]>("/integrations");
      return response.data;
    },
  });
}

export function useIntegrationCatalogQuery<
  T extends IntegrationCatalogEntry,
>() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  return useQuery(
    integrationCatalogQueryOptions<T>(credentialGetter, activeOrgId),
  );
}
