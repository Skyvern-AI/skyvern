import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { InfiniteData, useInfiniteQuery } from "@tanstack/react-query";

interface UseInfiniteBrowserProfilesQueryParams {
  page_size?: number;
  searchKey?: string;
  enabled?: boolean;
}

// Dedupe by id so concurrent-insert page-boundary repeats don't duplicate rows;
// module-scoped to keep the select reference stable across renders.
function dedupeProfilePagesById<TPageParam>(
  data: InfiniteData<Array<BrowserProfileApiResponse>, TPageParam>,
): InfiniteData<Array<BrowserProfileApiResponse>, TPageParam> {
  const seen = new Set<string>();
  return {
    ...data,
    pages: data.pages.map((page) =>
      page.filter((profile) => {
        if (seen.has(profile.browser_profile_id)) {
          return false;
        }
        seen.add(profile.browser_profile_id);
        return true;
      }),
    ),
  };
}

function useInfiniteBrowserProfilesQuery(
  params?: UseInfiniteBrowserProfilesQueryParams,
) {
  const credentialGetter = useCredentialGetter();
  const pageSize = params?.page_size ?? 20;
  const searchKey = params?.searchKey ?? "";

  return useInfiniteQuery<Array<BrowserProfileApiResponse>>({
    queryKey: ["browserProfiles-infinite", searchKey, pageSize],
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const searchParams = new URLSearchParams();
      searchParams.append("page", String(pageParam));
      searchParams.append("page_size", String(pageSize));
      if (searchKey) {
        searchParams.append("search_key", searchKey);
      }
      return client
        .get<Array<BrowserProfileApiResponse>>("/browser_profiles", {
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
    enabled: params?.enabled ?? true,
    select: dedupeProfilePagesById,
  });
}

export { useInfiniteBrowserProfilesQuery };
