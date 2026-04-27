import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
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
};

function useCredentialsQuery(props: Props = {}) {
  const { page = 1, page_size = 25, vault_type, ...queryOptions } = props;
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: ["credentials", page, page_size, vault_type],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("page", String(page));
      params.set("page_size", String(page_size));
      if (vault_type) {
        params.set("vault_type", vault_type);
      }
      return client.get("/credentials", { params }).then((res) => res.data);
    },
    refetchOnMount: "always",
    ...queryOptions,
  });
}

export { useCredentialsQuery };
