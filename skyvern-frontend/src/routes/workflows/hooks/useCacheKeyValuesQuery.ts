import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { keepPreviousData } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { CacheKeyValuesResponse } from "../types/scriptTypes";

type Props = {
  cacheKey?: string;
  filter?: string;
  page: number;
  workflowPermanentId?: string;
  debounceMs?: number;
};

function useCacheKeyValuesQuery({
  cacheKey,
  filter,
  page,
  workflowPermanentId,
  debounceMs = 300,
}: Props) {
  const credentialGetter = useCredentialGetter();

  // Debounce the filter parameter
  const [debouncedFilter, setDebouncedFilter] = useState(filter);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedFilter(filter);
    }, debounceMs);

    return () => clearTimeout(timer);
  }, [filter, debounceMs]);

  return useQuery<CacheKeyValuesResponse>({
    queryKey: [
      "cache-key-values",
      workflowPermanentId,
      cacheKey,
      page,
      debouncedFilter,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const cacheKeyEncoded = encodeURIComponent(cacheKey ?? "");
      let url = `/scripts/${workflowPermanentId}/${cacheKeyEncoded}/values?page=${page}&page_size=25`;

      if (debouncedFilter) {
        url += `&filter=${encodeURIComponent(debouncedFilter)}`;
      }

      const result = await client
        .get<CacheKeyValuesResponse>(url)
        .then((response) => response.data);

      return result;
    },
    enabled: !!workflowPermanentId && !!cacheKey && cacheKey.length > 0,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60 * 1000,
  });
}

export { useCacheKeyValuesQuery };
