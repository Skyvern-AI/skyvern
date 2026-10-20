import { create } from "zustand";

import type {
  FeedbackRateOptions,
  FeedbackRating,
} from "@/components/feedback/FeedbackThumbs";
import { captureRecordBrowser } from "@/util/recordBrowserTelemetry";

export type RecordingFeedbackTarget = {
  recording_attempt_id?: string;
  recording_id?: string;
  workflow_permanent_id: string;
  browser_session_id?: string;
  rating: FeedbackRating | null;
  responded: boolean;
};

type RecordingFeedbackStore = {
  target: RecordingFeedbackTarget | null;
  show: (ids: Omit<RecordingFeedbackTarget, "rating" | "responded">) => void;
  // Takes the target the user rated: the save is async and the prompt may change before it lands.
  rate: (
    target: RecordingFeedbackTarget,
    rating: FeedbackRating | null,
    reasonCode?: string,
    options?: FeedbackRateOptions,
  ) => void;
  dismiss: () => void;
  // The editor's own save ends the prompt; Copilot persists (retitles, auto-accepts) must not.
  retire: (workflowPermanentId: string) => void;
};

export const useRecordingFeedbackStore = create<RecordingFeedbackStore>(
  (set, get) => ({
    target: null,
    show: (ids) =>
      set({
        target: {
          ...ids,
          rating: null,
          responded: false,
        },
      }),
    rate: (target, rating, reasonCode, options) => {
      // Every id is passed explicitly so a newer attempt's live context can't leak in.
      captureRecordBrowser("record_browser.feedback_submitted", {
        recording_attempt_id: target.recording_attempt_id,
        recording_id: target.recording_id,
        workflow_permanent_id: target.workflow_permanent_id,
        browser_session_id: target.browser_session_id,
        rating,
        reason_code: rating === "down" ? reasonCode || undefined : undefined,
        // Typed text stays in our feedback table; analytics only learns that a reason exists.
        has_reason: rating === "down" && Boolean(reasonCode || options?.detail),
        updated: target.responded,
      });
      if (get().target === target)
        set({ target: { ...target, rating, responded: true } });
    },
    dismiss: () => set({ target: null }),
    retire: (workflowPermanentId) =>
      set((state) =>
        state.target?.workflow_permanent_id === workflowPermanentId
          ? { target: null }
          : {},
      ),
  }),
);
