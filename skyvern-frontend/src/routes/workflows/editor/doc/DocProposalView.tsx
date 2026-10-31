import {
  Fragment,
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Edge } from "@xyflow/react";
import { ChevronRightIcon, MagicWandIcon } from "@radix-ui/react-icons";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import type { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";
import { queryClient } from "@/api/QueryClient";
import { Button } from "@/components/ui/button";
import { humanizeBlockLabel } from "../../copilot/blockLabel";
import { useCopilotProposalStore } from "@/store/CopilotProposalStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { cn } from "@/util/utils";
import {
  ACCEPT_BUTTON_CLASS,
  REJECT_BUTTON_CLASS,
} from "../../copilot/cards/cardLayout";
import {
  DuplicateWriteRows,
  ReviewCoverageBadge,
  humanizedList,
  reviewCoverageTag,
} from "../../copilot/cards/ReviewEvidence";
import type { WorkflowBlock } from "../../types/workflowTypes";
import { apiWorkflowToSettings } from "../apiWorkflowToSettings";
import { getInitialParameters } from "../utils";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { diffWorkflowVersions } from "../panels/workflowReviewDiff";
import {
  convertWorkflowReviewBaseline,
  reviewBaselineFromSaveData,
} from "../workflowVersionFromSaveData";
import { workflowBlockTitle } from "../nodes/types";
import {
  getElements,
  getWorkflowBlocks,
  workflowEffectiveDefaultEngine,
} from "../workflowEditorUtils";
import type { DocNoteItem } from "./buildDocTree";
import {
  buildDocProposal,
  type DocNoteChange,
  type DocProposal,
  type DocRemovedStep,
} from "./docProposal";
import { DocText } from "./DocText";

const DocProposalContext = createContext<DocProposal | null>(null);

export const DocProposalProvider = DocProposalContext.Provider;

// The pending Copilot proposal for this workflow, rendered from what Accept would save.
// eslint-disable-next-line react-refresh/only-export-components
export function useDocProposal(
  workflowId: string | null,
  liveNodes: AppNode[],
  liveEdges: Edge[],
): DocProposal | null {
  const pending = useCopilotProposalStore((s) => s.proposal);
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const getSaveData = useWorkflowHasChangesStore((s) => s.getSaveData);
  const forThisWorkflow =
    pending &&
    (!workflowId || pending.proposed.workflow_permanent_id === workflowId)
      ? pending
      : null;
  // The live canvas is the baseline only when nothing was staged on it.
  // Without a proposal there is nothing to compare, so the live graph is not serialized.
  const liveBaselineNodes =
    !forThisWorkflow || forThisWorkflow.baseline ? null : liveNodes;
  const proposed = forThisWorkflow?.proposed;
  const stagedBaseline = forThisWorkflow?.baseline ?? null;
  // The chat republishes the proposal whenever its gate state changes, so these key on the proposal
  // itself; keying on the store object would re-run the baseline conversion on every republish.
  const liveBaseline = useMemo(() => {
    if (!proposed || stagedBaseline) return null;
    const saveData = getSaveData();
    return saveData ? reviewBaselineFromSaveData(saveData) : null;
  }, [proposed, stagedBaseline, getSaveData]);
  const elements = useMemo(() => {
    if (!proposed) return null;
    const { nodes, edges } = getElements(
      JSON.parse(
        JSON.stringify(proposed.workflow_definition?.blocks ?? []),
      ) as WorkflowBlock[],
      apiWorkflowToSettings(proposed),
      false,
      workflowEffectiveDefaultEngine(proposed, queryClient),
    );
    return { nodes, edges };
  }, [proposed]);
  const baselineBlocks = useMemo(
    () =>
      stagedBaseline
        ? (stagedBaseline.workflow_definition?.blocks ?? [])
        : liveBaseline
          ? (liveBaseline.workflow_definition?.blocks ?? [])
          : liveBaselineNodes
            ? getWorkflowBlocks(liveBaselineNodes, liveEdges)
            : [],
    [stagedBaseline, liveBaseline, liveBaselineNodes, liveEdges],
  );
  const sourceBaseline = useMemo(() => {
    if (!proposed) return null;
    return (
      stagedBaseline ??
      liveBaseline ?? {
        ...proposed,
        workflow_definition: {
          ...proposed.workflow_definition,
          blocks: baselineBlocks as WorkflowBlock[],
        },
      }
    );
  }, [stagedBaseline, liveBaseline, proposed, baselineBlocks]);
  const [converted, setConverted] = useState<{
    source: WorkflowVersion;
    orgId: string | undefined;
    baseline?: WorkflowVersion;
    error?: string;
  } | null>(null);
  useEffect(() => {
    if (!sourceBaseline) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const baseline = await convertWorkflowReviewBaseline(
          client,
          sourceBaseline,
          controller.signal,
        );
        if (!controller.signal.aborted)
          setConverted({ source: sourceBaseline, orgId, baseline });
      } catch (error) {
        if (!controller.signal.aborted)
          setConverted({
            source: sourceBaseline,
            orgId,
            error:
              error instanceof Error ? error.message : "Conversion failed.",
          });
      }
    })();
    return () => controller.abort();
  }, [sourceBaseline, credentialGetter, orgId]);
  const comparison =
    converted?.source === sourceBaseline && converted?.orgId === orgId
      ? converted
      : null;
  return useMemo(() => {
    if (!elements || !proposed || !forThisWorkflow) return null;
    const proposedBlocks = getWorkflowBlocks(elements.nodes, elements.edges);
    const baseline = comparison?.baseline ?? proposed;
    return {
      ...buildDocProposal({
        nodes: elements.nodes,
        edges: elements.edges,
        proposedBlocks,
        baselineBlocks: comparison?.baseline
          ? baseline.workflow_definition.blocks
          : proposedBlocks,
        diff: diffWorkflowVersions(baseline, proposed),
        review: forThisWorkflow.review,
        tested: forThisWorkflow.tested ?? false,
        description: proposed.description?.trim() ? proposed.description : null,
        parameters: getInitialParameters(proposed),
      }),
      comparisonPending: !comparison,
      comparisonError: comparison?.error,
    };
  }, [elements, proposed, forThisWorkflow, comparison]);
}

function plural(count: number, word: string) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function DocProposalCard({
  proposal,
  goTo,
}: {
  proposal: DocProposal;
  goTo: (nodeId: string) => void;
}) {
  const pending = useCopilotProposalStore((s) => s.proposal);
  const [open, setOpen] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const idByLabel = new Map(
    proposal.nodes.flatMap((n) =>
      isWorkflowBlockNode(n) ? [[n.data.label, n.id] as const] : [],
    ),
  );
  const marked = [...proposal.marks.entries()].filter(([label]) =>
    idByLabel.has(label),
  );
  const added = marked.filter(([, mark]) => mark === "added").length;
  const changed = marked.length - added;
  const noteSteps = proposal.notes.size;
  const settingsChanges = proposal.settingsChanges.filter(
    (change) => change.key !== "description",
  );
  const summaryParts = [
    changed ? `${plural(changed, "step")} changed` : null,
    added ? `${added} added` : null,
    proposal.removed.length ? `${proposal.removed.length} removed` : null,
    proposal.inputs.length
      ? `${plural(proposal.inputs.length, "input")} changed`
      : null,
    noteSteps ? `notes on ${plural(noteSteps, "step")}` : null,
    settingsChanges.length
      ? `${plural(settingsChanges.length, "workflow setting")} changed`
      : null,
    proposal.descriptionChange
      ? `workflow description ${
          proposal.descriptionChange.after === null
            ? "removed"
            : proposal.descriptionChange.before === null
              ? "added"
              : "changed"
        }`
      : null,
  ].filter((part): part is string => part !== null);
  const decide = async (decision: "approve" | "reject") => {
    if (!pending || deciding) return;
    setDeciding(true);
    try {
      await pending.decide(decision);
    } finally {
      setDeciding(false);
    }
  };
  const locked = !pending?.actionable || deciding;
  const comparisonUnavailable =
    proposal.comparisonPending || Boolean(proposal.comparisonError);
  return (
    <section
      aria-label="Copilot proposal"
      className="mb-5 rounded-lg border border-sky-500/30 bg-sky-500/[0.06]"
    >
      {/* One line unless the title cannot sit beside the buttons; then they wrap below it. */}
      <div className="flex min-h-10 min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-min flex-1 basis-0 items-start gap-2 rounded-md py-1 pl-1 text-left text-xs leading-4 outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            aria-hidden="true"
            className={cn(
              "size-4 shrink-0 text-sky-700 transition-transform dark:text-sky-300",
              open && "rotate-90",
            )}
          />
          {/* The summary wraps under the title, between its counts, rather than being cut off. */}
          <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="flex shrink-0 items-center gap-2">
              <MagicWandIcon
                aria-hidden="true"
                className="size-3.5 shrink-0 text-sky-700 dark:text-sky-300"
              />
              <span className="font-semibold text-sky-700 dark:text-sky-300">
                Copilot proposal
              </span>
            </span>
            {!comparisonUnavailable && summaryParts.length > 0 && (
              <span className="flex min-w-0 flex-wrap gap-x-1 text-muted-foreground">
                {summaryParts.map((part, index) => (
                  <Fragment key={part}>
                    {index > 0 && " "}
                    <span className="whitespace-nowrap">
                      {index < summaryParts.length - 1 ? `${part} ·` : part}
                    </span>
                  </Fragment>
                ))}
              </span>
            )}
          </span>
        </button>
        <div className="ml-auto flex shrink-0 gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={locked}
            onClick={() => void decide("reject")}
            className={cn("h-7 shrink-0 px-2.5", REJECT_BUTTON_CLASS)}
          >
            Reject
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={locked || !pending?.acceptable || comparisonUnavailable}
            onClick={() => void decide("approve")}
            className={cn("h-7 shrink-0 px-2.5", ACCEPT_BUTTON_CLASS)}
          >
            Accept
          </Button>
        </div>
      </div>
      {proposal.comparisonPending && (
        <p role="status" className="px-3 pb-2 text-xs text-muted-foreground">
          Checking proposal changes…
        </p>
      )}
      {proposal.comparisonError && (
        <p role="alert" className="px-3 pb-2 text-xs text-destructive">
          Couldn't compare this proposal: {proposal.comparisonError}. Use the
          chat card to Accept or Reject.
        </p>
      )}
      {open && !comparisonUnavailable && (
        <div className="flex flex-col gap-1 border-t border-sky-500/20 px-3 pb-2.5 pt-2 text-[13px]">
          {marked.map(([label, mark]) => (
            <button
              key={label}
              type="button"
              onClick={() => goTo(idByLabel.get(label)!)}
              className="flex min-w-0 items-center gap-2 rounded text-left outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
            >
              <MarkChip mark={mark} />
              <span className="truncate" title={label}>
                {humanizeBlockLabel(label)}
              </span>
            </button>
          ))}
          {proposal.removed.map((step) => (
            <div key={step.label} className="flex min-w-0 items-center gap-2">
              <MarkChip mark="removed" />
              <span
                className="truncate text-muted-foreground line-through"
                title={step.label}
              >
                {humanizeBlockLabel(step.label)}
              </span>
            </div>
          ))}
          {proposal.descriptionChange && (
            <div className="flex flex-col gap-1 border-l-2 border-amber-500/50 pl-3">
              <MarkChip
                mark={
                  proposal.descriptionChange.before === null
                    ? "added"
                    : proposal.descriptionChange.after === null
                      ? "removed"
                      : "changed"
                }
              />
              {proposal.descriptionChange.before !== null && (
                <p className="text-muted-foreground line-through">
                  <DocText value={proposal.descriptionChange.before} />
                </p>
              )}
              {proposal.descriptionChange.after !== null && (
                <p className="text-muted-foreground">
                  <DocText value={proposal.descriptionChange.after} />
                </p>
              )}
            </div>
          )}
          {settingsChanges.length > 0 && (
            <div className="text-muted-foreground">
              <p>Workflow settings changed:</p>
              <ul className="ml-4 list-disc">
                {settingsChanges.map((change) => (
                  <li key={change.key}>
                    {change.label}: {change.before ?? "Not set"} →{" "}
                    {change.after ?? "Not set"}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {proposal.inputs.length > 0 && (
            <div className="text-muted-foreground">
              <p>Workflow inputs changed:</p>
              <ul className="ml-4 list-disc">
                {proposal.inputs.map((input) => (
                  <li key={input.key}>
                    <span className="inline-flex flex-wrap items-center gap-1.5">
                      <MarkChip
                        mark={input.status === "new" ? "added" : input.status}
                      />
                      <code>{input.key}</code>
                      {input.detail && <span>({input.detail})</span>}
                    </span>
                    {input.changes.map((change) => (
                      <p key={change.key} className="ml-2">
                        {change.label}: {change.before ?? "Not set"} →{" "}
                        {change.after ?? "Not set"}
                      </p>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {proposal.review && (
            <div className="flex flex-col gap-1">
              {proposal.review.blocks.map((block) => {
                const tag = reviewCoverageTag(block, proposal.tested);
                return tag ? (
                  <div key={block.label} className="flex items-center gap-2">
                    <span>{humanizeBlockLabel(block.label)}</span>
                    <ReviewCoverageBadge label={tag} />
                  </div>
                ) : null;
              })}
              <DuplicateWriteRows groups={proposal.review.duplicateWrites} />
            </div>
          )}
          {proposal.keptNotes.length > 0 && (
            <p className="text-muted-foreground">
              Kept your notes on {humanizedList(proposal.keptNotes)}.
            </p>
          )}
          {proposal.keptDescriptions.length > 0 && (
            <p className="text-muted-foreground">
              Kept your descriptions on{" "}
              {humanizedList(proposal.keptDescriptions)}.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

const MARK_CLASSES = {
  added:
    "border-emerald-500/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  changed:
    "border-amber-500/30 bg-amber-500/15 text-amber-700 dark:text-amber-300",
  removed: "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400",
} as const;

const MARK_TEXT = { added: "Added", changed: "Changed", removed: "Removed" };

function MarkChip({ mark }: { mark: keyof typeof MARK_CLASSES }) {
  return (
    <span
      className={cn(
        "inline-flex h-[18px] shrink-0 items-center rounded border px-1.5 text-[11px] font-semibold",
        MARK_CLASSES[mark],
      )}
    >
      {MARK_TEXT[mark]}
    </span>
  );
}

function RemovedStep({ step }: { step: DocRemovedStep }) {
  const title =
    workflowBlockTitle[step.blockType as keyof typeof workflowBlockTitle] ??
    step.blockType;
  return (
    <div
      data-doc-removed={step.label}
      className="flex flex-col gap-1 border-l-2 border-red-500/50 pl-3"
    >
      {step.notes && (
        <p className="text-base leading-[1.55] text-muted-foreground line-through">
          <DocText value={step.notes} />
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
        <MarkChip mark="removed" />
        <span className="rounded bg-muted px-[7px] py-px font-semibold text-muted-foreground line-through">
          {title}
        </span>
        <span className="text-muted-foreground line-through">{step.label}</span>
      </div>
      {step.trailingNotes && (
        <p className="text-base leading-[1.55] text-muted-foreground line-through">
          <DocText value={step.trailingNotes} />
        </p>
      )}
    </div>
  );
}

function RemovedNote({ change }: { change: DocNoteChange }) {
  return (
    <div className="border-l-2 border-red-500/50 pl-3">
      <MarkChip mark="removed" />
      <p className="mt-0.5 text-base leading-[1.55] text-muted-foreground line-through">
        {change.before}
      </p>
    </div>
  );
}

// Removed steps with no surviving neighbour or branch have nowhere else to show.
export function DocUnanchoredRemovals() {
  const proposal = useContext(DocProposalContext);
  const branchIds = new Set(
    proposal?.nodes.flatMap((node) =>
      node.type === "conditional"
        ? node.data.branches.map((branch) => branch.id)
        : [],
    ) ?? [],
  );
  const orphans =
    proposal?.removed.filter(
      (step) =>
        !step.anchor ||
        (step.anchor.side === "branch" && !branchIds.has(step.anchor.branchId)),
    ) ?? [];
  if (!orphans.length) return null;
  return (
    <div className="mt-3 flex flex-col gap-3">
      {orphans.map((step) => (
        <RemovedStep key={step.label} step={step} />
      ))}
    </div>
  );
}

export function DocProposalBranchRemovals({
  branchId,
}: {
  branchId: string | undefined;
}) {
  const proposal = useContext(DocProposalContext);
  const removals =
    proposal?.removed.filter(
      (step) =>
        step.anchor?.side === "branch" && step.anchor.branchId === branchId,
    ) ?? [];
  if (!removals.length) return null;
  return (
    <div className="flex flex-col gap-3">
      {removals.map((step) => (
        <RemovedStep key={step.label} step={step} />
      ))}
    </div>
  );
}

export function DocProposalLoopRemovals({ loopLabel }: { loopLabel: string }) {
  const proposal = useContext(DocProposalContext);
  const removals =
    proposal?.removed.filter(
      (step) => step.anchor?.side === "loop" && step.anchor.label === loopLabel,
    ) ?? [];
  if (!removals.length) return null;
  return (
    <div className="flex flex-col gap-3">
      {removals.map((step) => (
        <RemovedStep key={step.label} step={step} />
      ))}
    </div>
  );
}

// Wraps one step of the Doc: its Added/Changed mark, removed neighbours, and removed notes.
export function DocProposalStepMark({
  node,
  children,
}: {
  node: AppNode | undefined;
  children: ReactNode;
}) {
  const proposal = useContext(DocProposalContext);
  if (!proposal || !node || !isWorkflowBlockNode(node)) return <>{children}</>;
  const label = node.data.label;
  const mark = proposal.marks.get(label);
  const notes = proposal.notes.get(label);
  const before = proposal.removed.filter(
    (r) => r.anchor?.label === label && r.anchor.side === "before",
  );
  const after = proposal.removed.filter(
    (r) => r.anchor?.label === label && r.anchor.side === "after",
  );
  return (
    <div className="flex flex-col gap-3">
      {before.map((step) => (
        <RemovedStep key={step.label} step={step} />
      ))}
      {notes?.above && notes.above.after === null && (
        <RemovedNote change={notes.above} />
      )}
      <div
        className={cn(
          mark && "border-l-2 pl-3",
          mark === "added" && "border-emerald-500/60",
          mark === "changed" && "border-amber-500/60",
        )}
      >
        {mark && <MarkChip mark={mark} />}
        {children}
      </div>
      {notes?.trailing && notes.trailing.after === null && (
        <RemovedNote change={notes.trailing} />
      )}
      {after.map((step) => (
        <RemovedStep key={step.label} step={step} />
      ))}
    </div>
  );
}

// Wraps one note of the Doc: added or changed notes are marked, with the old text struck through.
export function DocProposalNoteMark({
  item,
  byId,
  children,
}: {
  item: DocNoteItem;
  byId: Map<string, AppNode>;
  children: ReactNode;
}) {
  const proposal = useContext(DocProposalContext);
  const owner = byId.get(item.ownerNodeId);
  if (!proposal || !owner || !isWorkflowBlockNode(owner))
    return <>{children}</>;
  const label = owner.data.label;
  const change =
    proposal.notes.get(label)?.[
      item.placement === "above" ? "above" : "trailing"
    ];
  const kept = item.placement === "above" && proposal.keptNotes.includes(label);
  if (!change && !kept) return <>{children}</>;
  return (
    <div
      className={cn(
        "border-l-2 pl-3",
        change
          ? change.before === null
            ? "border-emerald-500/60"
            : "border-amber-500/60"
          : "border-border",
      )}
    >
      {change ? (
        <MarkChip mark={change.before === null ? "added" : "changed"} />
      ) : (
        <span className="text-[11px] font-semibold text-muted-foreground">
          Kept your note
        </span>
      )}
      {children}
      {change?.before && (
        <p className="text-[13.5px] leading-[1.55] text-muted-foreground line-through">
          <DocText value={change.before} />
        </p>
      )}
    </div>
  );
}
