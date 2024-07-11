import { getClient } from "@/api/AxiosClient";
import { WorkflowApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  workflowPermanentId: string;
};

function WorkflowTitle({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();

  const {
    data: workflow,
    isError,
    isLoading,
  } = useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}`)
        .then((response) => response.data);
    },
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
