import { useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { WorkflowBlock, WorkflowParameter } from "../types/workflowTypes";

type SopToBlocksResponse = {
  blocks: Array<WorkflowBlock>;
  parameters: Array<WorkflowParameter>;
};

type UseSopToBlocksMutationOptions = {
  onSuccess?: (result: SopToBlocksResponse) => void;
};

function useSopToBlocksMutation({ onSuccess }: UseSopToBlocksMutationOptions) {
  const credentialGetter = useCredentialGetter();
  const abortControllerRef = useRef<AbortController | null>(null);

  const mutation = useMutation({
    mutationFn: async (file: File) => {
      // Create new AbortController for this request
      abortControllerRef.current = new AbortController();

      const formData = new FormData();
      formData.append("file", file);
      const client = await getClient(credentialGetter);
      return (
        await client.post<SopToBlocksResponse>(
          "/workflows/sop-to-blocks",
          formData,
          {
            headers: {
              "Content-Type": "multipart/form-data",
            },
            signal: abortControllerRef.current.signal,
          },
        )
      ).data;
    },
    onSuccess: (result) => {
      toast({
        variant: "success",
        title: "SOP converted",
        description: `Generated ${result.blocks.length} block${result.blocks.length === 1 ? "" : "s"}`,
      });
      onSuccess?.(result);
    },
    onError: (error) => {
      // Don't show error toast if request was cancelled
      if (error instanceof AxiosError && error.code === "ERR_CANCELED") {
        toast({
          variant: "default",
          title: "Upload cancelled",
          description: "SOP conversion was cancelled.",
        });
        return;
      }

      // Graceful degradation for 404 (backend not deployed yet)
      if (error instanceof AxiosError && error.response?.status === 404) {
        toast({
          variant: "destructive",
          title: "Feature not yet available",
          description:
            "The Upload SOP feature is being deployed. Please try again later.",
        });
      } else {
        const message =
          error instanceof AxiosError
            ? error.response?.data?.detail || error.message
            : error instanceof Error
              ? error.message
              : "Failed to convert SOP";
        toast({
          variant: "destructive",
          title: "Failed to convert SOP",
          description: message,
        });
      }
    },
  });

  const cancel = () => {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
  };

  return { ...mutation, cancel };
}

export { useSopToBlocksMutation };
export type { SopToBlocksResponse };
