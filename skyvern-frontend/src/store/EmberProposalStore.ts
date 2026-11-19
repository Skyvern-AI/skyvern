import { create } from "zustand";
import type { ReviewProjection } from "@/routes/workflows/ember/narrativeState";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

export type EmberProposalDecision = "approve" | "reject";

export type PendingEmberProposal = {
  proposed: WorkflowApiResponse;
  // The pre-submit canvas when the draft was staged on it; null means the live canvas is the baseline.
  baseline: WorkflowApiResponse | null;
  review: ReviewProjection | null;
  tested?: boolean;
  // Whether the chat's own Accept and Reject would act now.
  actionable: boolean;
  // Accept also waits while the chat is turning auto-accept off; Reject does not.
  acceptable: boolean;
  // Settles through the chat exactly as its gate card and the comparison panel do.
  decide: (decision: EmberProposalDecision) => Promise<boolean>;
};

// Published by the Ember chat while the Doc editor is on, so the Doc view can show the pending
// proposal and every save path can refuse it.
export const useEmberProposalStore = create<{
  proposal: PendingEmberProposal | null;
  setProposal: (proposal: PendingEmberProposal | null) => void;
}>((set) => ({
  proposal: null,
  setProposal: (proposal) => set({ proposal }),
}));
