import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type QueryReturnType = Array<CredentialApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = UseQueryOptions;

function useCredentialsQuery(props: Props = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: ["credentials"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("page_size", "25");
      return client.get("/credentials", { params }).then((res) => res.data);
    },
    ...props,
  });
}

export { useCredentialsQuery };
