import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

function useCredentialsQuery() {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: ["credentials"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("page_size", "25");
      return client.get("/credentials", { params }).then((res) => res.data);
    },
  });
}

export { useCredentialsQuery };
