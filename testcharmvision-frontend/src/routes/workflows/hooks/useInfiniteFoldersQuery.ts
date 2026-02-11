import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useInfiniteQuery } from "@tanstack/react-query";
import type { Folder } from "../types/folderTypes";

interface UseInfiniteFoldersQueryParams {
  page_size?: number;
  search?: string;
}

function useInfiniteFoldersQuery(params?: UseInfiniteFoldersQueryParams) {
  const credentialGetter = useCredentialGetter();

  return useInfiniteQuery({
    queryKey: ["folders", "infinite", params],
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter);
      const searchParams = new URLSearchParams();

      searchParams.append("page", String(pageParam));

      if (params?.page_size) {
        searchParams.append("page_size", String(params.page_size));
      }
      if (params?.search) {
        searchParams.append("search", params.search);
      }

      return client
        .get<Array<Folder>>("/folders", { params: searchParams })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) => {
      // If the last page has items equal to page_size, there might be more
      const pageSize = params?.page_size || 10;
      if (lastPage.length === pageSize) {
        return allPages.length + 1;
      }
      return undefined;
    },
    initialPageParam: 1,
  });
}

export { useInfiniteFoldersQuery };
