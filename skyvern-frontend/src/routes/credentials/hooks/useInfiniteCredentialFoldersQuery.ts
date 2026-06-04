import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useInfiniteQuery } from "@tanstack/react-query";
import type { CredentialFolder } from "../types/credentialFolderTypes";

interface UseInfiniteCredentialFoldersQueryParams {
  page_size?: number;
  search?: string;
}

function useInfiniteCredentialFoldersQuery(
  params?: UseInfiniteCredentialFoldersQueryParams,
) {
  const credentialGetter = useCredentialGetter();
  const pageSize = params?.page_size ?? 20;

  return useInfiniteQuery({
    queryKey: ["credential-folders", "infinite", params],
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter);
      const searchParams = new URLSearchParams();

      searchParams.append("page", String(pageParam));
      searchParams.append("page_size", String(pageSize));
      if (params?.search) {
        searchParams.append("search", params.search);
      }

      return client
        .get<Array<CredentialFolder>>("/credential_folders", {
          params: searchParams,
        })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.length === pageSize) {
        return allPages.length + 1;
      }
      return undefined;
    },
    initialPageParam: 1,
  });
}

export { useInfiniteCredentialFoldersQuery };
