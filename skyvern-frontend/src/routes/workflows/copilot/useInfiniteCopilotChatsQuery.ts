import { useInfiniteQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowCopilotChatSummary } from "./workflowCopilotTypes";

interface UseInfiniteCopilotChatsQueryParams {
  workflow_permanent_id?: string;
  search?: string;
  page_size?: number;
  enabled?: boolean;
}

function useInfiniteCopilotChatsQuery({
  workflow_permanent_id,
  search,
  page_size = 20,
  enabled = true,
}: UseInfiniteCopilotChatsQueryParams = {}) {
  const credentialGetter = useCredentialGetter();

  return useInfiniteQuery({
    queryKey: [
      "copilot-chats",
      "infinite",
      { workflow_permanent_id, search, page_size },
    ],
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const searchParams = new URLSearchParams();
      searchParams.append("page", String(pageParam));
      searchParams.append("page_size", String(page_size));
      if (workflow_permanent_id) {
        searchParams.append("workflow_permanent_id", workflow_permanent_id);
      }
      if (search) {
        searchParams.append("search", search);
      }
      return client
        .get<Array<WorkflowCopilotChatSummary>>("/workflow/copilot/chats", {
          params: searchParams,
        })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) => {
      // A full page implies a possible next one; an exact-multiple total costs one empty fetch.
      if (lastPage.length === page_size) {
        return allPages.length + 1;
      }
      return undefined;
    },
    initialPageParam: 1,
    enabled,
  });
}

export { useInfiniteCopilotChatsQuery };
