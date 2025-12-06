import { useParams } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useRecordingStore } from "@/store/useRecordingStore";
import { type WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import { type WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

const FAIL_QUIET_NO_EVENTS = "FAIL-QUIET:NO-EVENTS" as const;

const useProcessRecordingMutation = ({
  browserSessionId,
  onSuccess,
}: {
  browserSessionId: string | null;
  onSuccess?: (args: {
    blocks: Array<WorkflowBlock>;
    parameters: Array<WorkflowParameter>;
  }) => void;
}) => {
  const credentialGetter = useCredentialGetter();
  const recordingStore = useRecordingStore();
  const { workflowPermanentId } = useParams();

  const processRecordingMutation = useMutation({
    mutationFn: async () => {
      if (!browserSessionId) {
        throw new Error(
          "Cannot process recording without a valid browser session ID.",
        );
      }

      if (!workflowPermanentId) {
        throw new Error(
          "Cannot process recording without a valid workflow permanent ID.",
        );
      }

      const eventCount = recordingStore.getEventCount();

      if (eventCount === 0) {
        throw new Error(FAIL_QUIET_NO_EVENTS);
      }

      // (this flushes any pending events)
      const compressedChunks = await recordingStore.getCompressedChunks();

      // TODO: Replace this mock with actual API call when endpoint is ready
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post<
          { compressed_chunks: string[] },
          {
            data: {
              blocks: Array<WorkflowBlock>;
              parameters: Array<WorkflowParameter>;
            };
          }
        >(`/browser_sessions/${browserSessionId}/process_recording`, {
          compressed_chunks: compressedChunks,
          workflow_permanent_id: workflowPermanentId,
        })
        .then((response) => ({
          blocks: response.data.blocks,
          parameters: response.data.parameters,
        }));
    },
    onSuccess: ({ blocks, parameters }) => {
      recordingStore.clear();

      if (blocks && blocks.length > 0) {
        toast({
          variant: "success",
          title: "Recording Processed",
          description: "The recording has been successfully processed.",
        });

        onSuccess?.({ blocks, parameters: parameters });

        return;
      }

      toast({
        variant: "warning",
        title: "Recording Processed (No Blocks)",
        description: "No blocks could be created from the recording.",
      });
    },
    onError: (error) => {
      if (error instanceof Error && error.message === FAIL_QUIET_NO_EVENTS) {
        return;
      }

      toast({
        variant: "destructive",
        title: "Error Processing Recording",
        description: error.message,
      });
    },
  });

  return processRecordingMutation;
};

export { useProcessRecordingMutation };
