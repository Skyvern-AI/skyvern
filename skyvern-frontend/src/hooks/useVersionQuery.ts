import { getClient } from "@/api/AxiosClient";
import { useQuery } from "@tanstack/react-query";

type VersionResponse = {
  version: string;
};

function useVersionQuery() {
  return useQuery<VersionResponse>({
    queryKey: ["version"],
    queryFn: async () => {
      const client = await getClient(null);
      return client.get("/version").then((response) => response.data);
    },
    staleTime: 60 * 60 * 1000,
    gcTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export { useVersionQuery };
