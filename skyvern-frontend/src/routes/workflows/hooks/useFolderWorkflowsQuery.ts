import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useInfiniteQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";

const FOLDER_WORKFLOWS_PAGE_SIZE = 20;

// Lazily loads the workflows inside a single folder. The query only runs while
// the folder node is expanded (`enabled`), so collapsed folders cost nothing.
function useFolderWorkflowsQuery({
  folderId,
  enabled,
}: {
  folderId: string;
  enabled: boolean;
}) {
  const credentialGetter = useCredentialGetter();

  return useInfiniteQuery({
    queryKey: ["workflows", "folder", folderId, FOLDER_WORKFLOWS_PAGE_SIZE],
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(pageParam));
      params.append("page_size", String(FOLDER_WORKFLOWS_PAGE_SIZE));
      params.append("only_workflows", "true");
      params.append("folder_id", folderId);
      return client
        .get<Array<WorkflowApiResponse>>("/workflows", { params })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === FOLDER_WORKFLOWS_PAGE_SIZE
        ? allPages.length + 1
        : undefined,
    initialPageParam: 1,
    enabled,
  });
}

export { useFolderWorkflowsQuery, FOLDER_WORKFLOWS_PAGE_SIZE };
