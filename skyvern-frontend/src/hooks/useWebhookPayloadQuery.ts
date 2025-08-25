import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  runId: string | undefined;
  enabled?: boolean;
};

function useWebhookPayloadQuery({ runId, enabled = true }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<Record<string, any>>({
    queryKey: ["webhookPayload", runId],
    queryFn: async () => {
      if (!runId) {
        throw new Error("Run ID is required");
      }
      const client = await getClient(credentialGetter);
      return client
        .get(`/runs/${runId}/webhook_payload`)
        .then((response) => response.data);
    },
    enabled: enabled && !!runId,
    retry: 1, // Only retry once since this is for display purposes
    staleTime: 5 * 60 * 1000, // 5 minutes - webhook payload doesn't change frequently
  });
}

export { useWebhookPayloadQuery };
