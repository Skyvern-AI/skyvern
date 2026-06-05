import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useSearchParams } from "react-router-dom";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";
import {
  buildDraftWorkflowApiResponse,
  isDraftWorkflowPermanentId,
} from "../draftWorkflow";

type Props = {
  workflowPermanentId?: string;
};

type DraftLocationState = {
  draftTitle?: string;
  draftRunWith?: "agent" | "code";
};

function useWorkflowQuery({ workflowPermanentId }: Props) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const isDraft = isDraftWorkflowPermanentId(workflowPermanentId);

  return useQuery<WorkflowApiResponse>({
    queryKey: [
      "workflow",
      workflowPermanentId,
      isDraft ? searchParams.toString() : null,
      isDraft ? location.state : null,
    ],
    queryFn: async () => {
      if (isDraft) {
        const locationState = location.state as DraftLocationState | null;
        const folderId = searchParams.get("folder_id");
        return buildDraftWorkflowApiResponse({
          title: locationState?.draftTitle,
          run_with: locationState?.draftRunWith,
          folder_id: folderId ?? undefined,
        });
      }

      const client = await getClient(credentialGetter);
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}`, { params })
        .then((response) => response.data);
    },
    enabled: isDraft ? true : !!globalWorkflows && !!workflowPermanentId,
    placeholderData: (previousData) => previousData,
  });
}

export { useWorkflowQuery };
