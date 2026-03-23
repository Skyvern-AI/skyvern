import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import type { PinScriptResponse } from "../types/scriptTypes";

type Props = {
  workflowPermanentId: string;
};

function usePinScriptMutation({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation<
    PinScriptResponse,
    AxiosError<{ detail?: string }>,
    { cacheKeyValue: string; pin: boolean }
  >({
    mutationFn: async ({ cacheKeyValue, pin }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const action = pin ? "pin" : "unpin";
      return client
        .post<PinScriptResponse>(`/scripts/${workflowPermanentId}/${action}`, {
          cache_key_value: cacheKeyValue,
        })
        .then((response) => response.data);
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: ["workflow-scripts", workflowPermanentId],
      });
      toast({
        title: data.is_pinned ? "Script pinned" : "Script unpinned",
        variant: "success",
      });
    },
    onError: (error) => {
      const detail = error.response?.data?.detail;
      toast({
        title: "Failed to update pin status",
        description: detail ?? error.message,
        variant: "destructive",
      });
    },
  });
}

export { usePinScriptMutation };
