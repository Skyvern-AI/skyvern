import { getClient } from "@/api/AxiosClient";
import {
  ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT,
  type CredentialApiResponse,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

function useImportedOnePasswordCredentialsQuery(enabled = true) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: ["credentials", "onepassword-imported"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const credentials: Array<CredentialApiResponse> = [];

      for (let page = 1; ; page += 1) {
        const response = await client.get<Array<CredentialApiResponse>>(
          "/credentials",
          {
            params: {
              page,
              page_size: ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT,
              vault_type: "onepassword",
            },
          },
        );
        credentials.push(...response.data);
        if (response.data.length < ONEPASSWORD_CREDENTIALS_REQUEST_LIMIT) {
          return credentials;
        }
      }
    },
    enabled,
  });
}

export { useImportedOnePasswordCredentialsQuery };
