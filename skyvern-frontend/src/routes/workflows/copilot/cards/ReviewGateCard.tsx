import { MagicWandIcon } from "@radix-ui/react-icons";

import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import { humanizeBlockLabel } from "../blockLabel";
import { everyTestBlockExecuted, hasFailedTestBlock } from "../copilotPhases";
import {
  isBuildTestConnectFailureState,
  TurnNarrativeState,
  ranCleanOnCurrentSource,
} from "../narrativeState";
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
  if (turn && hasFailedTestBlock(turn)) {
    return "untested";
  }
  // A tested claim needs the turn's own facts behind it, so an absent bundle, partial
  // coverage or a halted turn reads as untested rather than falling through to green.
  const covered = ranCleanOnCurrentSource(turn?.turnFacts ?? null);
  if (
    covered &&
    (turn?.proposalDisposition === "review_tested" ||
      turn?.proposalDisposition === "auto_applicable")
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
  if (legacy._copilot_unvalidated) {
    return "untested";
  }
  // With no turn there are no facts to project from, so a still-pending gate stays
  // silent rather than inventing either verdict.
  if (!turn) {
    return null;
  }
  return covered ? "tested" : "untested";
}

const VERDICT_PILL_CLASSES: Record<"tested" | "untested", string> = {
  tested:
    "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  untested: "border-sky-500/30 bg-sky-500/15 text-sky-700 dark:text-sky-300",
};

const END_TO_END_REAL_ACTIONS =
  "Testing end-to-end performs real actions on the site, so it can submit forms, " +
  "place orders, or send messages for real.";

const END_TO_END_EXPLAINER =
  "Each step was tested on its own — the steps have not been run together yet. " +
  END_TO_END_REAL_ACTIONS;

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
  onTestEndToEnd?: () => void;
  gateId?: string;
  // Transient highlight when the pending-proposal chip scrolls to this gate.
  flash?: boolean;
}

const REVIEW_SECTIONS = [
  { change: "added", title: "Added", prefix: "+", titleClass: "text-success" },
  {
    change: "changed",
    title: "Changed",
    prefix: "~",
    titleClass: "text-amber-700 dark:text-amber-300",
  },
  {
    change: "unchanged",
    title: "Unchanged",
    prefix: "",
    titleClass: "text-muted-foreground",
  },
  {
    change: "removed",
    title: "Removed",
    prefix: "-",
    titleClass: "text-destructive",
  },
] as const;

function humanizedList(labels: string[]): string {
  const names = labels.map(humanizeBlockLabel);
  if (names.length < 2) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
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
  onTestEndToEnd,
  gateId,
  flash = false,
}: ReviewGateCardProps) {
  const draft = turn?.draft ?? null;
  const rejected = settled === "rejected";
  const accepted = settled === "accepted";
  const itemClassName = rejected
    ? "ml-2 text-xs text-muted-foreground dark:text-slate-500 line-through opacity-60"
    : "ml-2 text-xs text-foreground";
  const review = turn?.review ?? null;
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
      {review ? (
        <div className="px-3 pb-3">
          {REVIEW_SECTIONS.map((section) => {
            const rows = review.blocks.filter(
              (block) => block.change === section.change,
            );
            if (rows.length === 0) return null;
            return (
              <div className="mt-2" key={section.change}>
                <div
                  className={`text-[10px] font-bold uppercase tracking-wide ${section.titleClass}`}
                >
                  {section.title}
                </div>
                {rows.map((block) => (
                  <div
                    key={`${block.change}-${block.label}`}
                    className={`${itemClassName} flex items-center gap-2`}
                    title={block.label}
                  >
                    <span>
                      {section.prefix ? `${section.prefix} ` : ""}
                      {humanizeBlockLabel(block.label)}
                    </span>
                    {block.coverage === "different_source" ? (
                      <span className="rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
                        Different source
                      </span>
                    ) : block.coverage === "unknown" ? (
                      <span className="rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
                        Not tested under this name
                      </span>
                    ) : block.coverage === "never_run" ||
                      (block.coverage === undefined && block.neverTested) ? (
                      <span className="rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
                        Never tested
                      </span>
                    ) : null}
                  </div>
                ))}
              </div>
            );
          })}
          {review.duplicateWrites.map((group) => (
            <div
              key={`${group.blockType}-${group.blockLabels.join("-")}`}
              className="mt-3 rounded-md border border-amber-500/25 bg-amber-500/10 px-2.5 py-2 text-xs text-foreground"
            >
              {humanizedList(group.blockLabels)} write to the same destination.
            </div>
          ))}
        </div>
      ) : draft ? (
        <div className="px-3 pb-3">
          {draft.blockLabels.length > 0 ? (
            <div className="mt-2">
              <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
                Proposed blocks
              </div>
              {draft.blockLabels.map((label) => (
                <div key={label} className={itemClassName} title={label}>
                  {humanizeBlockLabel(label)}
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
            className="rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-slate-elevation4 dark:text-slate-200"
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
            className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-slate-elevation4 hover:text-foreground dark:hover:text-slate-200"
          >
            Always accept
          </button>
          <button
            type="button"
            onClick={onReject}
            className="rounded-md px-3 py-1.5 text-xs text-red-700 hover:bg-red-500/10 hover:text-red-800 dark:text-red-300 dark:hover:text-red-400"
          >
            Reject
          </button>
          {onTestEndToEnd ? (
            <button
              type="button"
              onClick={onTestEndToEnd}
              className="rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-slate-elevation4 dark:text-slate-200"
            >
              {isBuildTestConnectFailureState(turn?.turnFacts?.terminalCause)
                ? "Retry in a fresh session"
                : "Test end-to-end"}
            </button>
          ) : null}
          {onTestEndToEnd ? (
            <p className="basis-full text-[11px] leading-snug text-muted-foreground">
              {verdict === "untested" &&
              turn &&
              everyTestBlockExecuted(turn) &&
              !hasFailedTestBlock(turn)
                ? END_TO_END_EXPLAINER
                : END_TO_END_REAL_ACTIONS}
            </p>
          ) : null}
        </div>
      ) : null}
      {settled ? (
        <div
          className={`flex items-center gap-2 border-l-2 px-3 py-2 text-xs ${
            accepted
              ? "border-l-success text-foreground dark:text-slate-200"
              : "border-l-slate-600 text-muted-foreground"
          }`}
        >
          <span
            className={`flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded text-[11px] ${
              accepted
                ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                : "bg-slate-elevation4 text-muted-foreground"
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
