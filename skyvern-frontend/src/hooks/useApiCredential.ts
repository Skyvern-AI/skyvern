import { ApiKeyApiResponse, OrganizationApiResponse } from "@cloud/types";
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "./useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { envCredential } from "@/util/env";

function useApiCredential() {
  const credentialGetter = useCredentialGetter();
  const credentialsFromEnv = envCredential;

  const { data: organizations } = useQuery<Array<OrganizationApiResponse>>({
    queryKey: ["organizations"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get("/organizations/")
        .then((response) => response.data.organizations);
    },
    enabled: envCredential === null,
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
