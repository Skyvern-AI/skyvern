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
};

function useCredentialsQuery(props: Props = {}) {
  const { page = 1, page_size = 25, ...queryOptions } = props;
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<CredentialApiResponse>>({
    queryKey: ["credentials", page, page_size],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.set("page", String(page));
      params.set("page_size", String(page_size));
      return client.get("/credentials", { params }).then((res) => res.data);
    },
    ...queryOptions,
  });
}

export { useCredentialsQuery };
