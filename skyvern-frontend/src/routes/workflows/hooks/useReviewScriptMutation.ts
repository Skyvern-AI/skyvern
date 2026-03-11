import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";

import type {
  ReviewScriptRequest,
  ReviewScriptResponse,
} from "../types/scriptTypes";

type Props = {
  workflowPermanentId: string;
  onSuccess?: (data: ReviewScriptResponse) => void;
};

function useReviewScriptMutation({ workflowPermanentId, onSuccess }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation<
    ReviewScriptResponse,
    AxiosError<{ detail?: string }>,
    ReviewScriptRequest
  >({
    mutationFn: async (request) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post<ReviewScriptResponse>(
          `/scripts/${workflowPermanentId}/review`,
          request,
        )
        .then((response) => response.data);
    },
    onSuccess: (data) => {
      // Invalidate script version queries globally (keyed by scriptId, not workflowPermanentId)
      queryClient.invalidateQueries({
        queryKey: ["script-versions"],
      });
      // Invalidate block-scripts scoped to this workflow (keyed by workflowPermanentId)
      queryClient.invalidateQueries({
        queryKey: ["block-scripts", workflowPermanentId],
      });
      onSuccess?.(data);
    },
    onError: (error) => {
      const detail = error.response?.data?.detail;
      toast({
        title: "Failed to fix script",
        description:
          detail ?? error.message ?? "An error occurred. Please try again.",
        variant: "destructive",
      });
    },
  });
}

export { useReviewScriptMutation };
