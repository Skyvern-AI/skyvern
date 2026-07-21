import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ApiKeyApiResponse, OrganizationApiResponse } from "@/api/types";
import { getRuntimeApiKey } from "@/util/env";
import { useCredentialGetter } from "./useCredentialGetter";

type ApiCredentialStatus = {
  apiKey: string | null;
  isPending: boolean;
  isError: boolean;
};

function useApiCredentialStatus(): ApiCredentialStatus {
  const credentialGetter = useCredentialGetter();
  const credentialsFromEnv = getRuntimeApiKey();

  const {
    data: organizations,
    isPending: organizationsPending,
    isError: organizationsError,
  } = useQuery<Array<OrganizationApiResponse>>({
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

  const {
    data: apiKeys,
    isPending: apiKeysPending,
    isError: apiKeysError,
  } = useQuery<Array<ApiKeyApiResponse>>({
    queryKey: ["apiKeys", organizationId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get(`/organizations/${organizationId}/apikeys`)
        .then((response) => response.data.api_keys);
    },
    enabled: !!organizationId, // don't run this until organization id exists
  });

  if (credentialsFromEnv !== null) {
    return {
      apiKey: credentialsFromEnv,
      isPending: false,
      isError: false,
    };
  }

  return {
    apiKey: apiKeys?.[0]?.token ?? null,
    isPending: organizationsPending || (!!organizationId && apiKeysPending),
    isError: organizationsError || (!!organizationId && apiKeysError),
  };
}

function useApiCredential() {
  return useApiCredentialStatus().apiKey;
}

export { useApiCredential, useApiCredentialStatus };
