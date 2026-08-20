import { useMutation } from "@tanstack/react-query";
import { useRef } from "react";
import { useFeatureFlagEnabled } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { RECORD_BROWSER_CODE_FIRST_FLAG } from "@/util/featureFlags";
import {
  useRecordingStore,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import {
  type WorkflowBlock,
  type WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";
import {
  captureRecordBrowser,
  markRecordBrowserProcessed,
} from "@/util/recordBrowserTelemetry";

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
  const workflowPermanentId = useWorkflowPermanentId();
  const mutationStartedAtRef = useRef<number | null>(null);
  // Per-user opt-in preview; not enrolled reads as false (agent blocks).
  const codeFirst =
    useFeatureFlagEnabled(RECORD_BROWSER_CODE_FIRST_FLAG) ?? false;

  const processRecordingMutation = useMutation({
    mutationFn: async (
      variables: {
        /**
         * Live-interpreted draft steps (with user edits/deletes applied).
         * When provided the backend converts them deterministically instead
         * of re-processing the raw event stream.
         */
        draftSteps?: Array<RecordingDraftStep> | null;
      } | void,
    ) => {
      const draftSteps = variables?.draftSteps ?? null;

      if (!browserSessionId) {
        throw new Error(
          "Cannot process recording without a valid browser session ID.",
        );
      }

      if (!workflowPermanentId) {
        throw new Error(
          "Cannot process recording without a valid agent permanent ID.",
        );
      }

      mutationStartedAtRef.current = Date.now();

      const eventCount = recordingStore.getEventCount();
      const hasDraftSteps = (draftSteps?.length ?? 0) > 0;

      if (eventCount === 0 && !hasDraftSteps) {
        captureRecordBrowser("record_browser.empty_blocked", {
          seconds_recording: recordingStore.getSecondsRecording(),
        });
        throw new Error(FAIL_QUIET_NO_EVENTS);
      }

      const compressedChunks = await recordingStore.getCompressedChunks();

      captureRecordBrowser("record_browser.process_attempted", {
        event_count: recordingStore.getEventCount(),
        compressed_chunk_count: compressedChunks.length,
        draft_step_count: draftSteps?.length,
      });

      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post<
          {
            compressed_chunks: string[];
            draft_steps?: Array<RecordingDraftStep>;
            code_first: boolean;
          },
          {
            data: {
              blocks: Array<WorkflowBlock>;
              parameters: Array<WorkflowParameter>;
            };
          }
        >(`/browser_sessions/${browserSessionId}/process_recording`, {
          compressed_chunks: compressedChunks,
          workflow_permanent_id: workflowPermanentId,
          code_first: codeFirst,
          ...(draftSteps !== null ? { draft_steps: draftSteps } : {}),
        })
        .then((response) => ({
          blocks: response.data.blocks,
          parameters: response.data.parameters,
        }));
    },
    onSuccess: ({ blocks, parameters }) => {
      const latencyMs =
        mutationStartedAtRef.current !== null
          ? Date.now() - mutationStartedAtRef.current
          : 0;
      mutationStartedAtRef.current = null;

      markRecordBrowserProcessed(blocks?.length ?? 0);

      captureRecordBrowser("record_browser.processed", {
        block_count: blocks?.length ?? 0,
        parameter_count: parameters?.length ?? 0,
        latency_ms: latencyMs,
      });

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

      // A zero-block commit still ends the session: the caller's onSuccess (which
      // normally exits recording after landing blocks) is skipped, and without
      // this the user is stranded in the recording panel with a dead Done button.
      recordingStore.setIsRecording(false);

      toast({
        variant: "warning",
        title: "Recording Processed (No Blocks)",
        description: "No blocks could be created from the recording.",
      });
    },
    onError: (error) => {
      const latencyMs =
        mutationStartedAtRef.current !== null
          ? Date.now() - mutationStartedAtRef.current
          : 0;
      mutationStartedAtRef.current = null;

      if (error instanceof Error && error.message === FAIL_QUIET_NO_EVENTS) {
        recordingStore.reset();
        toast({
          variant: "warning",
          title: "Nothing was recorded",
          description:
            "Interact with the live browser (clicks, typing, navigation), then stop recording again to generate blocks.",
        });
        return;
      }

      captureRecordBrowser("record_browser.processing_failed", {
        error_message: error instanceof Error ? error.message : String(error),
        latency_ms: latencyMs,
      });

      useRecordingStore.setState({ finishRequested: false });

      toast({
        variant: "destructive",
        title: "Error Processing Recording",
        description: error instanceof Error ? error.message : String(error),
      });
    },
  });

  return processRecordingMutation;
};

export { useProcessRecordingMutation };
