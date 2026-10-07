import { Cross2Icon } from "@radix-ui/react-icons";

import { getClient } from "@/api/AxiosClient";
import {
  FeedbackThumbs,
  type FeedbackRateOptions,
  type FeedbackRating,
} from "@/components/feedback/FeedbackThumbs";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useUser } from "@/hooks/useUser";
import { useRecordingFeedbackStore } from "@/store/RecordingFeedbackStore";

const REASON_OPTIONS = [
  { value: "missing_steps", label: "Missing steps" },
  { value: "extra_or_wrong_steps", label: "Extra or wrong steps" },
  { value: "wrong_values", label: "Wrong values or parameters" },
  { value: "credential_handling", label: "Credential handling" },
  { value: "navigation", label: "Navigation" },
  { value: "workflow_failed", label: "Generated workflow failed" },
  { value: "other", label: "Other" },
];

export function RecordingFeedbackPrompt({
  workflowPermanentId,
}: {
  workflowPermanentId?: string;
}) {
  const target = useRecordingFeedbackStore((state) => state.target);
  const { rate, dismiss } = useRecordingFeedbackStore.getState();
  const credentialGetter = useCredentialGetter();
  const { get: getUser } = useUser();

  if (!target || target.workflow_permanent_id !== workflowPermanentId) {
    return null;
  }

  return (
    <div
      role="group"
      aria-label="Recording feedback"
      className="flex items-start justify-between gap-2 rounded-lg border border-border bg-slate-elevation2 px-3 py-2"
      data-testid="recording-feedback-prompt"
    >
      <FeedbackThumbs
        key={target.recording_attempt_id ?? target.recording_id}
        rating={target.rating}
        onRate={async (
          rating: FeedbackRating | null,
          reasonCode?: string,
          options?: FeedbackRateOptions,
        ) => {
          // Without a durable recording id there is no row to attach the reason to; analytics still records the rating.
          if (target.recording_id) {
            const client = await getClient(credentialGetter, "sans-api-v1");
            await client.post("/feedback", {
              target_type: "browser_recording",
              target_id: target.recording_id,
              rating,
              reason: options?.detail || reasonCode || null,
              submitted_by: getUser()?.email ?? null,
            });
          }
          rate(target, rating, reasonCode, options);
        }}
        prompt="Were these recorded steps helpful?"
        reasonOptions={REASON_OPTIONS}
        className="min-w-0 flex-1"
      />
      <button
        type="button"
        aria-label="Dismiss recording feedback"
        onClick={dismiss}
        className="ml-1 inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <Cross2Icon className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
