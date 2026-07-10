import { MagicWandIcon } from "@radix-ui/react-icons";

import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import { TurnNarrativeState } from "../narrativeState";
import { getDiffCardTitle } from "./diffCardTitle";

export type ReviewGateVerdict = "tested" | "untested" | null;
export type ReviewGateSettled = "accepted" | "rejected" | null;

// The backend stashes this marker on chat.proposed_workflow only for
// review_untested proposals; it's absent from the typed WorkflowApiResponse
// shape because it never round-trips through a real workflow save.
type LegacyProposedWorkflow = WorkflowApiResponse & {
  _copilot_unvalidated?: boolean;
};

// eslint-disable-next-line react-refresh/only-export-components
export function getReviewGateVerdict(
  turn: TurnNarrativeState | undefined,
  proposedWorkflow: WorkflowApiResponse | null,
): ReviewGateVerdict {
  if (
    turn?.proposalDisposition === "review_tested" ||
    turn?.proposalDisposition === "auto_applicable"
  ) {
    return "tested";
  }
  if (turn?.proposalDisposition) {
    return "untested";
  }
  if (!proposedWorkflow) {
    return null;
  }
  const legacy = proposedWorkflow as LegacyProposedWorkflow;
  return legacy._copilot_unvalidated ? "untested" : "tested";
}

const VERDICT_PILL_CLASSES: Record<"tested" | "untested", string> = {
  tested: "border-emerald-500/30 bg-emerald-500/15 text-emerald-300",
  untested: "border-sky-500/30 bg-sky-500/15 text-sky-300",
};

const VERDICT_PILL_LABELS: Record<"tested" | "untested", string> = {
  tested: "Tested",
  untested: "Untested",
};

interface ReviewGateCardProps {
  turn?: TurnNarrativeState;
  pending: boolean;
  verdict: ReviewGateVerdict;
  settled?: ReviewGateSettled;
  actionsEnabled: boolean;
  onAccept: () => void;
  onAlwaysAccept: () => void;
  onReject: () => void;
  onReview: () => void;
  gateId?: string;
  // Transient highlight when the pending-proposal chip scrolls to this gate.
  flash?: boolean;
}

export function ReviewGateCard({
  turn,
  pending,
  verdict,
  settled = null,
  actionsEnabled,
  onAccept,
  onAlwaysAccept,
  onReject,
  onReview,
  gateId,
  flash = false,
}: ReviewGateCardProps) {
  const draft = turn?.draft ?? null;
  const rejected = settled === "rejected";
  const accepted = settled === "accepted";
  const itemClassName = rejected
    ? "ml-2 text-xs text-slate-500 line-through opacity-60"
    : "ml-2 text-xs text-foreground";
  const priorLabels = new Set(
    (turn?.blocks ?? [])
      .filter((block) => block.state === "drafted")
      .map((block) => block.label),
  );
  const removed = draft
    ? [...priorLabels].filter((label) => !draft.blockLabels.includes(label))
    : [];
  const title = turn
    ? getDiffCardTitle(turn, { pendingProposal: pending, rejected, accepted })
    : "Proposed changes";

  return (
    <div
      id={gateId}
      className={`overflow-hidden rounded-[10px] border border-border bg-slate-elevation2 ${
        flash ? "ring-2 ring-sky-400/60 [transition:box-shadow_1.1s]" : ""
      }`}
    >
      <div className="flex items-center gap-2 px-3 pt-3 text-xs font-semibold text-foreground">
        <MagicWandIcon className="h-3.5 w-3.5" />
        {title}
        {pending && verdict ? (
          <span
            className={`ml-auto rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${VERDICT_PILL_CLASSES[verdict]}`}
          >
            {VERDICT_PILL_LABELS[verdict]}
          </span>
        ) : null}
      </div>
      {draft ? (
        <div className="px-3 pb-3">
          {draft.blockLabels.length > 0 ? (
            <div className="mt-2">
              <div className="text-[10px] font-bold uppercase tracking-wide text-success">
                Added
              </div>
              {draft.blockLabels.map((label) => (
                <div key={label} className={itemClassName}>
                  + {label}
                </div>
              ))}
            </div>
          ) : null}
          {removed.length > 0 ? (
            <div className="mt-2">
              <div className="text-[10px] font-bold uppercase tracking-wide text-destructive">
                Removed
              </div>
              {removed.map((label) => (
                <div key={label} className={itemClassName}>
                  - {label}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {pending && actionsEnabled ? (
        <div className="flex flex-wrap gap-2 border-t border-border/55 bg-slate-elevation1/55 px-3 py-2">
          <button
            type="button"
            onClick={onReview}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-elevation4"
          >
            Review
          </button>
          <button
            type="button"
            onClick={onAccept}
            className="rounded-md bg-success px-3 py-1.5 text-xs font-semibold text-success-foreground hover:opacity-90"
          >
            Accept
          </button>
          <button
            type="button"
            onClick={onAlwaysAccept}
            className="rounded-md px-3 py-1.5 text-xs text-slate-400 hover:bg-slate-elevation4 hover:text-slate-200"
          >
            Always accept
          </button>
          <button
            type="button"
            onClick={onReject}
            className="rounded-md px-3 py-1.5 text-xs text-red-300 hover:bg-red-500/10 hover:text-red-400"
          >
            Reject
          </button>
        </div>
      ) : null}
      {settled ? (
        <div
          className={`flex items-center gap-2 border-l-2 px-3 py-2 text-xs ${
            accepted
              ? "border-l-success text-slate-200"
              : "border-l-slate-600 text-slate-400"
          }`}
        >
          <span
            className={`flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded text-[11px] ${
              accepted
                ? "bg-emerald-500/15 text-emerald-400"
                : "bg-slate-elevation4 text-slate-400"
            }`}
          >
            {accepted ? "✓" : "↺"}
          </span>
          {accepted
            ? "Accepted — saved as a new workflow version"
            : "Discarded — canvas reverted to the previous version"}
        </div>
      ) : null}
    </div>
  );
}
