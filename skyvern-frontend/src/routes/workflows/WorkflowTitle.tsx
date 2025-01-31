import { getClient } from "@/api/AxiosClient";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { apiPathPrefix } from "@/util/env";
import { useGlobalWorkflowsQuery } from "./hooks/useGlobalWorkflowsQuery";

type Props = {
  workflowPermanentId: string;
};

function WorkflowTitle({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();

  const {
    data: workflow,
    isError,
    isLoading,
  } = useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`${apiPathPrefix}/workflows/${workflowPermanentId}`, { params })
        .then((response) => response.data);
    },
    enabled: !!globalWorkflows && !!workflowPermanentId,
  });

  if (isLoading) {
    return <Skeleton className="h-6 w-full" />;
  }

  if (isError || !workflow) {
    return <span></span>;
  }

  return <span>{workflow.title}</span>;
}

export { WorkflowTitle };
