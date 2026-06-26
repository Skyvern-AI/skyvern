import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { CredentialFolder } from "../types/credentialFolderTypes";

interface UseCredentialFoldersQueryParams {
  page?: number;
  page_size?: number;
  search?: string;
}

function useCredentialFoldersQuery(params?: UseCredentialFoldersQueryParams) {
  const credentialGetter = useCredentialGetter();

  return useQuery({
    queryKey: ["credential-folders", params],
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
        .get<Array<CredentialFolder>>("/credential_folders", {
          params: searchParams,
        })
        .then((response) => response.data);
    },
  });
}

export { useCredentialFoldersQuery };
