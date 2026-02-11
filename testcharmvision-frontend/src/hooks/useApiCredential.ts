import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "./useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { getRuntimeApiKey } from "@/util/env";
import { ApiKeyApiResponse, OrganizationApiResponse } from "@/api/types";

function useApiCredential() {
  const credentialGetter = useCredentialGetter();
  const credentialsFromEnv = getRuntimeApiKey();

  const { data: organizations } = useQuery<Array<OrganizationApiResponse>>({
    queryKey: ["organizations"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get("/organizations/")
        .then((response) => response.data.organizations);
    },
    enabled: credentialsFromEnv === null,
  });

  const organization = organizations?.[0];
  const organizationId = organization?.organization_id;

  const { data: apiKeys } = useQuery<Array<ApiKeyApiResponse>>({
    queryKey: ["apiKeys", organizationId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get(`/organizations/${organizationId}/apikeys`)
        .then((response) => response.data.api_keys);
    },
    enabled: !!organizationId, // don't run this until organization id exists
  });

  return credentialsFromEnv ?? apiKeys?.[0]?.token ?? null;
}

export { useApiCredential };
