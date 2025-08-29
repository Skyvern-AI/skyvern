import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  workflowPermanentId?: string;
};

function useDebugSessionBlockOutputsQuery({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<{ [k: string]: { extracted_information: unknown } }>({
    queryKey: ["block-outputs", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const result = await client
        .get(`/debug-session/${workflowPermanentId}/block-outputs`)
        .then((response) => response.data);
      return result;
    },
    enabled: !!workflowPermanentId,
  });
}

export { useDebugSessionBlockOutputsQuery };
