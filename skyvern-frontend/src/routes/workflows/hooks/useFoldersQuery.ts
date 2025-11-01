import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { Folder } from "../types/folderTypes";

interface UseFoldersQueryParams {
  page?: number;
  page_size?: number;
  search?: string;
}

function useFoldersQuery(params?: UseFoldersQueryParams) {
  const credentialGetter = useCredentialGetter();

  return useQuery({
    queryKey: ["folders", params],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const searchParams = new URLSearchParams();

      if (params?.page) {
        searchParams.append("page", String(params.page));
      }
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
  });
}

export { useFoldersQuery };
