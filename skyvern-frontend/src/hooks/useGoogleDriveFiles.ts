import { useInfiniteQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { GoogleDriveFile } from "@/api/types";

type PagedGoogleDriveFiles = {
  files: GoogleDriveFile[];
  next_page_token: string | null;
  incomplete_search?: boolean;
};

type Options = {
  credentialId: string;
  folders: boolean;
  query: string;
  enabled: boolean;
};

const PAGE_SIZE = 25;

export function useGoogleDriveFiles({
  credentialId,
  folders,
  query,
  enabled,
}: Options) {
  const credentialGetter = useCredentialGetter();

  return useInfiniteQuery<PagedGoogleDriveFiles>({
    queryKey: ["googleDrive", "files", credentialId, folders, query],
    queryFn: async ({ pageParam }) => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("credential_id", credentialId);
      params.append("folders", String(folders));
      params.append("page_size", String(PAGE_SIZE));
      if (query) params.append("q", query);
      if (pageParam) params.append("page_token", String(pageParam));
      const response = await client.get("/integrations/google/drive/files", {
        params,
      });
      return response.data as PagedGoogleDriveFiles;
    },
    getNextPageParam: (last) => last.next_page_token ?? undefined,
    initialPageParam: "",
    enabled: enabled && Boolean(credentialId),
  });
}
