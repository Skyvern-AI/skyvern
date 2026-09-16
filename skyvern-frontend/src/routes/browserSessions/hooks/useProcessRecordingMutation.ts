import { useMutation } from "@tanstack/react-query";
import { useRef } from "react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import {
  useRecordingStore,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import { type WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import type { RecordedParameter } from "@/store/RecordedBlocksStore";
import { useRecordingRefinementEvidenceStore } from "@/store/RecordingRefinementEvidenceStore";
import type { RecordingEvidencePacket } from "@/routes/workflows/copilot/workflowCopilotTypes";
import { useStudioPanes } from "@/routes/workflows/studio/useStudioPanes";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
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
    recordingId: string | null;
    blocks: Array<WorkflowBlock>;
    parameters: Array<RecordedParameter>;
  }) => void;
}) => {
  const credentialGetter = useCredentialGetter();
  const { openPane } = useStudioPanes();
  const recordingStore = useRecordingStore();
  const workflowPermanentId = useWorkflowPermanentId();
  const mutationStartedAtRef = useRef<number | null>(null);
  const recordingStatsRef = useRef<{
    transport: "cdp" | "vnc";
    durationMs: number;
    eventCount: number;
    optimisticStepCount: number;
  } | null>(null);
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

      if (useWorkflowHasChangesStore.getState().pendingRecordingId !== null) {
        throw new Error(
          "Save or discard the current workflow changes before processing another recording.",
        );
      }

      mutationStartedAtRef.current = Date.now();

      const currentRecording = useRecordingStore.getState();
      const eventCount = currentRecording.getEventCount();
      const recordingAttemptId = currentRecording.recordingAttemptId;
      const interpretationSessionId = currentRecording.interpretationSessionId;
      recordingStatsRef.current = {
        transport: currentRecording.recordingTransport,
        durationMs: Math.round(currentRecording.getSecondsRecording() * 1000),
        eventCount,
        optimisticStepCount: currentRecording.optimisticSteps.length,
      };
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
            supports_credential_tokens: boolean;
            recording_attempt_id?: string;
            interpretation_session_id?: string;
          },
          {
            data: {
              recording_id: string | null;
              blocks: Array<WorkflowBlock>;
              parameters: Array<RecordedParameter>;
              evidence?: RecordingEvidencePacket | null;
            };
          }
        >(`/browser_sessions/${browserSessionId}/process_recording`, {
          compressed_chunks: compressedChunks,
          workflow_permanent_id: workflowPermanentId,
          // Keep opting in explicitly while older backends still honor this field.
          code_first: true,
          // This build substitutes credential tokens in a code block's code; a build that
          // does not must not be handed blocks whose code reads a token it cannot rename.
          supports_credential_tokens: true,
          ...(recordingAttemptId !== null
            ? { recording_attempt_id: recordingAttemptId }
            : {}),
          ...(interpretationSessionId !== null
            ? { interpretation_session_id: interpretationSessionId }
            : {}),
          ...(draftSteps !== null ? { draft_steps: draftSteps } : {}),
        })
        .then((response) => ({
          recordingId: response.data.recording_id ?? null,
          blocks: response.data.blocks,
          parameters: response.data.parameters,
          evidence: response.data.evidence ?? null,
        }));
    },
    onSuccess: ({ recordingId, blocks, parameters, evidence }) => {
      const latencyMs =
        mutationStartedAtRef.current !== null
          ? Date.now() - mutationStartedAtRef.current
          : 0;
      mutationStartedAtRef.current = null;

      markRecordBrowserProcessed(blocks?.length ?? 0);

      const completedRecording = recordingStatsRef.current;
      recordingStatsRef.current = null;
      const currentRecording = useRecordingStore.getState();
      captureRecordBrowser("record_browser.finished", {
        transport:
          completedRecording?.transport ?? currentRecording.recordingTransport,
        duration_ms:
          completedRecording?.durationMs ??
          Math.round(currentRecording.getSecondsRecording() * 1000),
        event_count:
          completedRecording?.eventCount ?? currentRecording.getEventCount(),
        optimistic_step_count:
          completedRecording?.optimisticStepCount ??
          currentRecording.optimisticSteps.length,
      });

      captureRecordBrowser("record_browser.processed", {
        block_count: blocks?.length ?? 0,
        parameter_count: parameters?.length ?? 0,
        latency_ms: latencyMs,
      });

      recordingStore.clear();

      if (blocks && blocks.length > 0) {
        if (recordingId && workflowPermanentId) {
          useWorkflowHasChangesStore
            .getState()
            .setPendingRecording(recordingId, workflowPermanentId);
        }
        toast({
          variant: "success",
          title: evidence ? "Recorded steps added" : "Recording processed",
          description: evidence
            ? "Copilot is refining the workflow now. Follow its progress in the Copilot pane."
            : "The recording has been successfully processed.",
        });

        onSuccess?.({ recordingId, blocks, parameters: parameters });

        if (evidence) {
          // One replace-navigation stores the packet and arms the copilot turn that
          // reads it, the same handoff RunTab makes for diagnose_run.
          const nonce = crypto.randomUUID();
          useRecordingRefinementEvidenceStore
            .getState()
            .set({ nonce, evidence });
          openPane("copilot", {
            state: { copilotAction: { kind: "refine_recording", nonce } },
          });
        }

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
      recordingStatsRef.current = null;

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
