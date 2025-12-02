import { useMutation } from "@tanstack/react-query";

// import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
// import { useCredentialGetter } from "@/hooks/useCredentialGetter";
// import { type MessageInExfiltratedEvent } from "@/store/useRecordingStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import {
  type ActionBlock,
  type WorkflowBlock,
} from "@/routes/workflows/types/workflowTypes";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const FAIL_QUITE_NO_EVENTS = "FAIL-QUIET:NO-EVENTS" as const;

const useProcessRecordingMutation = ({
  browserSessionId,
  onSuccess,
}: {
  browserSessionId: string | null;
  onSuccess?: (workflowBlocks: Array<WorkflowBlock>) => void;
}) => {
  // const credentialGetter = useCredentialGetter();
  const recordingStore = useRecordingStore();

  const processRecordingMutation = useMutation({
    mutationFn: async () => {
      if (!browserSessionId) {
        throw new Error(
          "Cannot process recording without a valid browser session ID.",
        );
      }

      const eventCount = recordingStore.getEventCount();

      if (eventCount === 0) {
        throw new Error(FAIL_QUITE_NO_EVENTS);
      }

      // (this flushes any pending events)
      const compressedChunks = await recordingStore.getCompressedChunks();

      // TODO: Replace this mock with actual API call when endpoint is ready
      // const client = await getClient(credentialGetter, "sans-api-v1");
      // return client
      //   .post<
      //     { compressed_chunks: string[] },
      //     { data: Array<WorkflowBlock> }
      //   >(`/browser_sessions/${browserSessionId}/process_recording`, {
      //     compressed_chunks: compressedChunks,
      //   })
      //   .then((response) => response.data);

      // Mock response with 2-second delay
      console.log(
        `Processing ${eventCount} events in ${compressedChunks.length} compressed chunks`,
      );
      await sleep(2000);

      // Return mock workflow blocks with two ActionBlocks
      const mockWorkflowBlocks: Array<WorkflowBlock> = [
        {
          block_type: "action",
          label: "action_1",
          title: "Enter search term",
          navigation_goal: "Enter 'foo' in the search field",
          url: null,
          error_code_mapping: null,
          parameters: [],
          engine: null,
          continue_on_failure: false,
          output_parameter: {
            parameter_type: "output",
            key: "action_1_output",
            description: null,
            output_parameter_id: "mock-output-1",
            workflow_id: browserSessionId || "mock-workflow-id",
            created_at: new Date().toISOString(),
            modified_at: new Date().toISOString(),
            deleted_at: null,
          },
          model: null,
        } satisfies ActionBlock,
        {
          block_type: "action",
          label: "action_2",
          title: "Click search",
          navigation_goal: "Click the search button",
          url: null,
          error_code_mapping: null,
          parameters: [],
          engine: null,
          continue_on_failure: false,
          output_parameter: {
            parameter_type: "output",
            key: "action_2_output",
            description: null,
            output_parameter_id: "mock-output-2",
            workflow_id: browserSessionId || "mock-workflow-id",
            created_at: new Date().toISOString(),
            modified_at: new Date().toISOString(),
            deleted_at: null,
          },
          model: null,
        } satisfies ActionBlock,
      ];
      return mockWorkflowBlocks;
    },
    onSuccess: (workflowBlocks) => {
      // Clear events after successful flush
      recordingStore.clear();

      toast({
        variant: "success",
        title: "Recording Processed",
        description: "The recording has been successfully processed.",
      });

      if (workflowBlocks) {
        onSuccess?.(workflowBlocks);
      }
    },
    onError: (error) => {
      if (error instanceof Error && error.message === FAIL_QUITE_NO_EVENTS) {
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
