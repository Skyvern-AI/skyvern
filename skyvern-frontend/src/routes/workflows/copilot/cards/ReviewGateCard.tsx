import {
  ChevronDownIcon,
  DotsHorizontalIcon,
  MagicWandIcon,
} from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";

import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import { humanizeBlockLabel } from "../blockLabel";
import { everyTestBlockExecuted, hasFailedTestBlock } from "../copilotPhases";
import {
  isBuildTestConnectFailureState,
  TurnNarrativeState,
  ranCleanOnCurrentSource,
} from "../narrativeState";
import {
  CardBody,
  CardFooter,
  CardHeader,
  CardPill,
  CopilotCard,
  GutterRow,
  type PillTone,
} from "./cardChrome";
import { ACCEPT_BUTTON_CLASS, REJECT_BUTTON_CLASS } from "./cardLayout";
import { draftLanded, getDiffCardTitle } from "./diffCardTitle";

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

const END_TO_END_REAL_ACTIONS =
  "Testing end-to-end performs real actions on the site, so it can submit forms, " +
  "place orders, or send messages for real.";

const PENDING_CHANGE_LIMIT = 4;

// Cause-coded per the 2026-07-13 ruling on terminal states: red only for a write that
// failed, amber for one whose outcome we cannot read yet. Most Accept "failures" are a
// second click on a first click that had already saved, so amber is the honest colour.
const GATE_STATUS: Record<
  "accepting" | "accept" | "changed" | "recover" | "reload" | "saved",
  {
    label: string;
    variant: BadgeProps["variant"];
    line: string;
    // For the gates that outlive their proposal: the same cause, told to a user who has no
    // proposal left to act on, so it names an exit instead of pointing at the card.
    lineWithoutProposal?: string;
  }
> = {
  accepting: {
    label: "Accepting…",
    variant: "secondary",
    line: "Saving your accepted changes.",
  },
  accept: {
    label: "Not saved",
    variant: "destructive",
    line: "Accept failed, so nothing was saved. The proposal is still pending.",
  },
  changed: {
    label: "Proposal changed",
    variant: "warning",
    line: "Nothing was saved because this proposal changed. Review it before accepting.",
    lineWithoutProposal:
      "This workflow changed, so your Accept didn't go through and the proposal is gone. Send Copilot a new message to propose it again.",
  },
  recover: {
    label: "Confirming…",
    variant: "warning",
    line: "This may have saved. Don't save the workflow until Copilot confirms.",
  },
  saved: {
    label: "Saved, not shown",
    variant: "warning",
    line: "Copilot saved this change, but the editor couldn't load it. Try again — this replaces what's on the canvas — or reload the page, which discards unsaved canvas edits.",
  },
  reload: {
    label: "Couldn't reload",
    variant: "warning",
    line: "Couldn't reload this proposal, so it may be out of date. Try again, or reload the page — reloading discards unsaved canvas edits.",
  },
};

interface ReviewGateCardProps {
  turn?: TurnNarrativeState;
  pending: boolean;
  verdict: ReviewGateVerdict;
  settled?: ReviewGateSettled;
  actionsEnabled: boolean;
  // Every action in the row acts on a staged proposal, so with none the row is locked: the gates
  // that outlive their proposal render for their message, not their buttons.
  hasProposal: boolean;
  // False only while this chat's Turn off is in flight: an Accept started then could write auto_accept
  // back on after it. Review and Reject write no auto_accept and stay available.
  acceptsEnabled?: boolean;
  onAccept: () => void;
  onAlwaysAccept: () => void;
  onReject: () => void;
  onReview: () => void;
  onTestEndToEnd?: () => void;
  accepting?: boolean;
  failure?: "accept" | "changed" | "recover" | "reload" | "saved" | null;
  onRetry?: () => void;
  gateId?: string;
  // Transient highlight when the pending-proposal chip scrolls to this gate.
  flash?: boolean;
}

type ChangeRow = {
  key: string;
  label: string;
  marker: string;
  srLabel?: string;
  markerClass: string;
  struck: boolean;
  muted: boolean;
  coverageTag: string | null;
  neverTested: boolean;
};

const CHANGE_ORDER = [
  {
    change: "added",
    marker: "+",
    srLabel: "Added",
    markerClass: "text-success",
  },
  {
    change: "changed",
    marker: "~",
    srLabel: "Changed",
    markerClass: "text-amber-700 dark:text-amber-300",
  },
  {
    change: "removed",
    marker: "−",
    srLabel: "Removed",
    markerClass: "text-destructive",
  },
  {
    change: "unchanged",
    marker: "",
    srLabel: "Unchanged",
    markerClass: "text-muted-foreground",
  },
] as const;

function changeRows(turn: TurnNarrativeState | undefined): ChangeRow[] {
  const review = turn?.review ?? null;
  if (review) {
    return CHANGE_ORDER.flatMap((section) =>
      review.blocks
        .filter((block) => block.change === section.change)
        .map((block) => {
          const neverTested =
            block.coverage === "never_run" ||
            (block.coverage === undefined && Boolean(block.neverTested));
          return {
            key: `${block.change}-${block.label}`,
            label: block.label,
            marker: section.marker,
            srLabel: section.srLabel,
            markerClass: section.markerClass,
            struck: section.change === "removed",
            muted: section.change === "unchanged",
            coverageTag:
              block.coverage === "different_source"
                ? "Different source"
                : block.coverage === "unknown"
                  ? "Not tested under this name"
                  : neverTested
                    ? "Never tested"
                    : null,
            neverTested,
          };
        }),
    );
  }
  return (turn?.draft?.blockLabels ?? []).map((label) => ({
    key: label,
    label,
    marker: "",
    markerClass: "text-muted-foreground",
    struck: false,
    muted: false,
    coverageTag: null,
    neverTested: false,
  }));
}

function blockCountLabel(rows: ChangeRow[]): string | null {
  const changed = rows.filter((row) => !row.muted).length;
  const count = changed > 0 ? changed : rows.length;
  if (count === 0) return null;
  return `${count} ${count === 1 ? "block" : "blocks"}`;
}

function humanizedList(labels: string[]): string {
  const names = labels.map(humanizeBlockLabel);
  if (names.length < 2) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

function ChangeList({
  turn,
  rows,
  limit,
  discarded,
}: {
  turn: TurnNarrativeState | undefined;
  rows: ChangeRow[];
  limit?: number;
  discarded: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  // A removal is never folded away, so a deletion cannot be accepted unseen.
  const capped = limit !== undefined && !showAll;
  const visible: ChangeRow[] = [];
  const hidden: ChangeRow[] = [];
  let shown = 0;
  for (const row of rows) {
    if (!capped || row.struck || shown < limit) {
      visible.push(row);
      if (!row.struck) shown += 1;
    } else {
      hidden.push(row);
    }
  }
  const hiddenNeverTested = hidden.filter((row) => row.neverTested).length;
  const duplicateWrites = turn?.review?.duplicateWrites ?? [];
  if (rows.length === 0 && duplicateWrites.length === 0) return null;
  return (
    <div>
      {visible.map((row) => (
        <GutterRow
          key={row.key}
          marker={row.marker}
          markerClass={row.markerClass}
          srLabel={row.srLabel}
        >
          <span
            title={row.label}
            className={
              discarded || row.struck
                ? "text-muted-foreground line-through"
                : row.muted
                  ? "text-muted-foreground"
                  : ""
            }
          >
            {humanizeBlockLabel(row.label)}
          </span>
          {row.coverageTag ? (
            <span className="ml-2 whitespace-nowrap rounded-full bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:text-sky-300">
              {row.coverageTag}
            </span>
          ) : null}
        </GutterRow>
      ))}
      {hidden.length > 0 || showAll ? (
        <div className="pl-7 pt-0.5">
          <button
            type="button"
            aria-expanded={showAll}
            onClick={() => setShowAll((value) => !value)}
            className="text-[11px] text-sky-700 hover:underline dark:text-sky-300"
          >
            {showAll
              ? "Show less"
              : `Show ${hidden.length} more${hiddenNeverTested ? ` · ${hiddenNeverTested} never tested` : ""}`}
          </button>
        </div>
      ) : null}
      {duplicateWrites.map((group) => (
        <GutterRow
          key={`${group.blockType}-${group.blockLabels.join("-")}`}
          marker="!"
          markerClass="font-bold text-amber-700 dark:text-amber-300"
          srLabel="Warning"
        >
          <span className="text-amber-700 dark:text-amber-300">
            {humanizedList(group.blockLabels)} write to the same destination.
          </span>
        </GutterRow>
      ))}
    </div>
  );
}

// One pill so the header never truncates: the most severe note, plus how many more the expanded
// card lists.
function appliedNotesPill(
  turn: TurnNarrativeState | undefined,
  rows: ChangeRow[],
) {
  const notes: { text: string; tone: PillTone }[] = [];
  if (turn && hasFailedTestBlock(turn)) {
    notes.push({ text: "Test failed", tone: "amber" });
  }
  const warnings = turn?.review?.duplicateWrites.length ?? 0;
  if (warnings > 0) {
    notes.push({
      text: `${warnings} ${warnings === 1 ? "warning" : "warnings"}`,
      tone: "amber",
    });
  }
  const neverTested = rows.filter((row) => row.neverTested).length;
  if (neverTested > 0) {
    notes.push({ text: `${neverTested} untested`, tone: "sky" });
  }
  const first = notes[0];
  if (!first) return null;
  return (
    <CardPill tone={first.tone}>
      {first.text}
      {notes.length > 1 ? (
        <>
          <span aria-hidden="true"> +{notes.length - 1}</span>
          <span className="sr-only">
            {` and ${notes.length - 1} more ${notes.length === 2 ? "note" : "notes"}`}
          </span>
        </>
      ) : null}
    </CardPill>
  );
}

function ResolvedReviewCard({
  turn,
  rows,
  rejected,
  accepted,
  title,
  gateId,
  flash,
}: {
  turn: TurnNarrativeState | undefined;
  rows: ChangeRow[];
  rejected: boolean;
  accepted: boolean;
  title: string;
  gateId?: string;
  flash: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const applied =
    accepted || (turn !== undefined && draftLanded(turn, { rejected }));
  const names =
    rows.length > 0 && rows.length <= 2
      ? rows.map((row) => humanizeBlockLabel(row.label)).join(", ")
      : blockCountLabel(rows);
  const hasDetail =
    rows.length > 0 || (turn?.review?.duplicateWrites.length ?? 0) > 0;
  return (
    <CopilotCard
      id={gateId}
      className={
        flash ? "ring-2 ring-sky-400/60 [transition:box-shadow_1.1s]" : ""
      }
    >
      <CardHeader
        icon={
          <>
            {rejected ? (
              <span
                aria-hidden="true"
                className="text-xs text-muted-foreground"
              >
                ↺
              </span>
            ) : applied ? (
              <span
                aria-hidden="true"
                className="text-xs font-bold text-emerald-600 dark:text-emerald-400"
              >
                ✓
              </span>
            ) : (
              <MagicWandIcon
                aria-hidden="true"
                className="h-3.5 w-3.5 text-muted-foreground"
              />
            )}
            {/* The glyph alone says nothing to a screen reader, and an auto-applied change
                shows the same glyph without a user decision behind it. */}
            {accepted || rejected ? (
              <span className="sr-only">
                {rejected
                  ? "Discarded, canvas reverted to the previous version"
                  : "Accepted and saved to the workflow"}
              </span>
            ) : null}
          </>
        }
        title={
          rejected ? (
            <span className="text-muted-foreground">Discarded changes</span>
          ) : (
            title
          )
        }
        meta={names}
        right={
          rejected ? (
            <span className="text-[11px] text-muted-foreground">
              Canvas reverted
            </span>
          ) : (
            appliedNotesPill(turn, rows)
          )
        }
        expanded={expanded}
        onToggle={hasDetail ? () => setExpanded((value) => !value) : undefined}
      />
      {expanded ? (
        <CardBody>
          <ChangeList turn={turn} rows={rows} discarded={rejected} />
        </CardBody>
      ) : null}
    </CopilotCard>
  );
}

export function ReviewGateCard({
  turn,
  pending,
  verdict,
  settled = null,
  actionsEnabled,
  hasProposal,
  acceptsEnabled = true,
  onAccept,
  onAlwaysAccept,
  onReject,
  onReview,
  onTestEndToEnd,
  accepting = false,
  failure = null,
  onRetry,
  gateId,
  flash = false,
}: ReviewGateCardProps) {
  const [confirmingTest, setConfirmingTest] = useState(false);
  const runTestRef = useRef<HTMLButtonElement>(null);
  const moreTriggerRef = useRef<HTMLButtonElement>(null);
  const bodyTestRef = useRef<HTMLButtonElement>(null);
  // The confirmation replaces the row that opened it, so focus is placed by hand both ways.
  const restoreFocusRef = useRef(false);
  const selectingTestRef = useRef(false);
  useEffect(() => {
    if (confirmingTest) {
      runTestRef.current?.focus();
    } else if (restoreFocusRef.current) {
      restoreFocusRef.current = false;
      (moreTriggerRef.current ?? bodyTestRef.current)?.focus();
    }
  }, [confirmingTest]);
  const closeConfirmation = () => {
    restoreFocusRef.current = true;
    setConfirmingTest(false);
  };
  const rows = changeRows(turn);
  const rejected = settled === "rejected";
  const accepted = settled === "accepted";
  const title = turn
    ? getDiffCardTitle(turn, { pendingProposal: pending, rejected, accepted })
    : "Proposed changes";

  if (!pending) {
    return (
      <ResolvedReviewCard
        turn={turn}
        rows={rows}
        rejected={rejected}
        accepted={accepted}
        title={title}
        gateId={gateId}
        flash={flash}
      />
    );
  }

  const gateStatus = failure
    ? GATE_STATUS[failure]
    : accepting
      ? GATE_STATUS.accepting
      : null;
  const billingCreditRefusal =
    turn?.turnFacts?.terminalCause === "billing_credit_admission_refusal";
  const testFailed = Boolean(turn && hasFailedTestBlock(turn));
  const showActions = actionsEnabled && hasProposal;
  const connectFailure = isBuildTestConnectFailureState(
    turn?.turnFacts?.terminalCause,
  );
  const canTest = Boolean(onTestEndToEnd) && !billingCreditRefusal;
  const testLabel = connectFailure
    ? "Retry in a fresh session"
    : "Test end-to-end";
  // The two cases where running end-to-end is the next step rather than an option: every block ran
  // on its own but never together, or the test could not start a browser at all.
  const offerTestInBody =
    canTest &&
    (connectFailure ||
      (verdict === "untested" &&
        turn !== undefined &&
        everyTestBlockExecuted(turn) &&
        !testFailed));
  const actionsLocked =
    accepting ||
    failure === "reload" ||
    failure === "recover" ||
    failure === "saved";
  // A lock that lands mid-confirmation would strand it behind the disabled fieldset.
  if (confirmingTest && actionsLocked) {
    setConfirmingTest(false);
  }
  const showTestInBody = showActions && offerTestInBody && !confirmingTest;
  const hasBody =
    rows.length > 0 ||
    (turn?.review?.duplicateWrites.length ?? 0) > 0 ||
    showTestInBody;

  const acceptSplit = acceptsEnabled ? (
    <div className="flex">
      <Button
        type="button"
        size="sm"
        onClick={onAccept}
        className={`${ACCEPT_BUTTON_CLASS} rounded-r-none`}
      >
        Accept
      </Button>
      <DropdownMenu>
        {/* disabled on the trigger itself: Radix opens on pointerdown, which a disabled fieldset
            does not stop. */}
        <DropdownMenuTrigger asChild disabled={actionsLocked}>
          <Button
            type="button"
            size="sm"
            aria-label="More accept options"
            className={`${ACCEPT_BUTTON_CLASS} w-8 rounded-l-none border-l border-black/15 px-0`}
          >
            <ChevronDownIcon className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-60">
          <DropdownMenuItem
            onSelect={onAlwaysAccept}
            className="flex-col items-start gap-0.5 text-xs"
          >
            Always accept
            <span className="text-[11px] leading-snug text-muted-foreground">
              Apply Copilot&apos;s future changes in this chat without asking.
            </span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  ) : null;

  const moreMenu =
    canTest && !offerTestInBody ? (
      <DropdownMenu>
        <DropdownMenuTrigger asChild disabled={actionsLocked}>
          <Button
            ref={moreTriggerRef}
            type="button"
            size="sm"
            variant="outline"
            aria-label="More actions"
            className="w-8 px-0"
          >
            <DotsHorizontalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="w-60"
          onCloseAutoFocus={(event) => {
            if (selectingTestRef.current) {
              selectingTestRef.current = false;
              event.preventDefault();
            }
          }}
        >
          <DropdownMenuItem
            onSelect={() => {
              selectingTestRef.current = true;
              setConfirmingTest(true);
            }}
            className="flex-col items-start gap-0.5 text-xs"
          >
            {testLabel}
            <span className="text-[11px] leading-snug text-muted-foreground">
              Runs every block together on the real site.
            </span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    ) : null;

  return (
    <CopilotCard
      id={gateId}
      className={
        flash ? "ring-2 ring-sky-400/60 [transition:box-shadow_1.1s]" : ""
      }
    >
      <CardHeader
        icon={<MagicWandIcon className="h-3.5 w-3.5 text-muted-foreground" />}
        title={title}
        meta={blockCountLabel(rows)}
        wrapTitle
        right={
          !verdict ? null : testFailed ? (
            <CardPill tone="amber">Test failed</CardPill>
          ) : verdict === "tested" ? (
            <CardPill tone="green">Tested</CardPill>
          ) : (
            <CardPill tone="sky">Untested</CardPill>
          )
        }
      />
      {hasBody ? (
        <CardBody>
          <ChangeList
            turn={turn}
            rows={rows}
            limit={PENDING_CHANGE_LIMIT}
            discarded={false}
          />
          {showTestInBody ? (
            <GutterRow marker="▷" markerClass="text-sky-600 dark:text-sky-400">
              <span className="text-muted-foreground">
                {connectFailure
                  ? null
                  : "Each step was tested on its own, but not together yet. "}
                <button
                  ref={bodyTestRef}
                  type="button"
                  disabled={actionsLocked}
                  onClick={() => setConfirmingTest(true)}
                  className="font-medium text-sky-700 hover:underline disabled:opacity-60 dark:text-sky-300"
                >
                  {testLabel}
                </button>
              </span>
            </GutterRow>
          ) : null}
        </CardBody>
      ) : null}
      {actionsEnabled ? (
        <CardFooter>
          {gateStatus ? (
            <div
              role={failure ? "alert" : "status"}
              className={`flex flex-wrap items-center gap-x-2 gap-y-1 ${hasProposal ? "mb-2" : ""}`}
            >
              <Badge
                variant={gateStatus.variant}
                className="shrink-0 px-1.5 py-0 text-[10px]"
              >
                {gateStatus.label}
              </Badge>
              <span className="min-w-0 flex-1 text-[11px] leading-snug text-muted-foreground">
                {(!hasProposal && gateStatus.lineWithoutProposal) ||
                  gateStatus.line}
              </span>
              {/* In recover and reload this is the only live control on the card, so it is a
                  button in its own right and sits outside the disabled action row. */}
              {onRetry ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={accepting}
                  onClick={onRetry}
                  className="shrink-0"
                >
                  Try again
                </Button>
              ) : null}
            </div>
          ) : null}
          {/* A second Accept while one is in flight loses the race server-side, and a
              proposal that could not be re-read may be stale, so the row stays locked. */}
          {hasProposal ? (
            <fieldset
              disabled={actionsLocked}
              className="min-w-0 disabled:opacity-60"
            >
              {billingCreditRefusal ? (
                <p className="pb-2 text-[11px] leading-snug text-muted-foreground">
                  No browser or run started because credits are exhausted.{" "}
                  <a
                    href="/billing"
                    className="font-medium text-sky-700 underline underline-offset-2 dark:text-sky-300"
                  >
                    Go to Billing
                  </a>
                  .
                </p>
              ) : null}
              {confirmingTest && canTest ? (
                <>
                  <p className="pb-2 text-xs leading-snug text-foreground dark:text-slate-200">
                    {END_TO_END_REAL_ACTIONS}
                  </p>
                  <div className="flex items-center gap-2">
                    <Button
                      ref={runTestRef}
                      type="button"
                      size="sm"
                      onClick={() => {
                        closeConfirmation();
                        onTestEndToEnd?.();
                      }}
                    >
                      Run test
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={closeConfirmation}
                    >
                      Cancel
                    </Button>
                  </div>
                </>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  {acceptSplit}
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={onReview}
                  >
                    Review
                  </Button>
                  <div className="ml-auto flex items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={onReject}
                      className={REJECT_BUTTON_CLASS}
                    >
                      Reject
                    </Button>
                    {moreMenu}
                  </div>
                </div>
              )}
            </fieldset>
          ) : null}
        </CardFooter>
      ) : null}
    </CopilotCard>
  );
}
