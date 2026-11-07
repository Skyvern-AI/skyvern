import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CodeIcon,
  DotsHorizontalIcon,
  MagicWandIcon,
} from "@radix-ui/react-icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/util/utils";
import { stripJinjaReferences } from "./docTextUtils";
import { type AppNode, type WorkflowBlockNode } from "../nodes";
import type { DocBranch, DocItem } from "./buildDocTree";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { docStepTitle } from "./docStepRegistry";
import { DocStepResult } from "./DocStepResult";
import { DocStepControls, DocTag, type DocStepPlace } from "./DocStep";
import { runStepByKey, type LineSplit } from "./docKeys";
import type { DocScopeRef } from "./docGraphEdits";
import { DocText } from "./DocText";
import { DocDeleteConfirm, DocEditableLine } from "./DocEditing";
import { useDocRunFor } from "./useDocRunFor";
import { docBranchSelection, selectedStepIds } from "./docBranchSelection";

const MAX_VISIBLE_CONDITIONS = 3;
const kindLabel = { if: "If", elseIf: "Else if", otherwise: "Otherwise" };

const shortLabel = (condition: string) => {
  const plain = stripJinjaReferences(condition).trim();
  return plain.length > 30 ? `${plain.slice(0, 29)}…` : plain;
};

const HOVER_TO_OPEN_MS = 500;

function BranchTab({
  branch,
  selected,
  ran,
  onSelect,
}: {
  branch: DocBranch;
  selected: boolean;
  ran: boolean;
  onSelect: () => void;
}) {
  const { dragId } = useContext(WorkflowDocActionsContext);
  // Holding a dragged step over a tab for a moment opens it, so its rows become drop targets.
  const hover = useRef<number | null>(null);
  const stopHover = () => {
    if (hover.current !== null) window.clearTimeout(hover.current);
    hover.current = null;
  };
  useEffect(() => stopHover, []);
  // A drag that ends over the tab sends no dragleave; don't switch branches after it.
  useEffect(() => {
    if (!dragId) stopHover();
  }, [dragId]);
  const label = branch.kind === "otherwise" ? "" : shortLabel(branch.condition);
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected}
      onClick={onSelect}
      onDragEnter={() => {
        if (!dragId || selected || hover.current !== null) return;
        hover.current = window.setTimeout(() => {
          hover.current = null;
          onSelect();
        }, HOVER_TO_OPEN_MS);
      }}
      onDragLeave={(event) => {
        if (
          !event.currentTarget.contains(event.relatedTarget as Element | null)
        )
          stopHover();
      }}
      onDrop={stopHover}
      title={branch.condition || undefined}
      className={cn(
        "flex min-w-0 max-w-[min(240px,100%)] items-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-0.5 py-[3px] text-[12.5px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
        selected && "border-[#8456A8] text-foreground dark:border-[#B793DA]",
      )}
    >
      <span className="shrink-0 font-semibold text-[#8456A8] dark:text-[#B793DA]">
        {kindLabel[branch.kind]}
      </span>
      {branch.kind !== "otherwise" &&
        (label ? (
          <span className="min-w-0 truncate">{label}</span>
        ) : (
          <span className="italic text-[#B45B0A] dark:text-[#E79A55]">
            no condition
          </span>
        ))}
      {branch.stepCount > 0 && (
        <span className="text-[11px] text-muted-foreground">
          {branch.stepCount}
        </span>
      )}
      {ran && (
        <span
          title="This branch ran"
          className="inline-flex shrink-0 items-center text-[11px] font-medium text-green-700 dark:text-green-400"
        >
          <CheckIcon className="size-3" aria-hidden />
          ran
        </span>
      )}
    </button>
  );
}

export function DocJump({
  targetId,
  byId,
  verb,
}: {
  targetId: string;
  byId: Map<string, AppNode>;
  verb: string;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const target = byId.get(targetId);
  if (!target) return null;
  return (
    <div className="mb-0.5 mt-1 flex flex-wrap items-center gap-1.5">
      <span className="text-[12.5px] text-muted-foreground">{verb}</span>
      <button
        type="button"
        onClick={() => actions.goTo(targetId)}
        title={docStepTitle(target)}
        className="inline-flex max-w-full items-center gap-1 rounded-md border border-border px-2 py-px text-[13px] font-[520] text-[#8456A8] outline-none hover:bg-[#F3EEF8] focus-visible:ring-2 focus-visible:ring-ring dark:text-[#B793DA] dark:hover:bg-[rgba(183,147,218,.15)]"
      >
        <ArrowRightIcon className="size-3 shrink-0" />
        <span className="truncate">{docStepTitle(target)}</span>
      </button>
    </div>
  );
}

export function DocConditional({
  node,
  item,
  byId,
  editable,
  showRunControl = editable,
  place,
  onEnter,
  renderChildren,
}: {
  node: WorkflowBlockNode;
  item: Extract<DocItem, { kind: "conditional" }>;
  byId: Map<string, AppNode>;
  editable: boolean;
  showRunControl?: boolean;
  place?: DocStepPlace;
  // Enter on the If line starts the line after the conditional.
  onEnter?: (split: LineSplit | null, key?: string) => void;
  renderChildren: (
    children: DocItem[],
    scope?: DocScopeRef,
    branchId?: string,
  ) => ReactNode;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const open = actions.conditionalOpen(node.id);
  const conditions = item.branches.filter((b) => b.kind !== "otherwise");
  const otherwise = item.branches.find((b) => b.kind === "otherwise");
  const { blocksByLabel, unknownLabels } = useDocRunFor(node.data.label);
  const {
    selected,
    ranBranch: ranOnRun,
    selectedRan,
  } = docBranchSelection(
    item,
    node.data.label,
    actions.selectedBranch(node.id),
    byId,
    blocksByLabel,
  );
  // A conditional edited since the run cannot say which branch ran.
  const ranBranch = unknownLabels.has(node.data.label) ? undefined : ranOnRun;
  let visible = conditions;
  let overflow: DocBranch[] = [];
  if (conditions.length > MAX_VISIBLE_CONDITIONS) {
    visible = conditions.slice(0, MAX_VISIBLE_CONDITIONS - 1);
    if (selected.kind !== "otherwise" && !visible.includes(selected))
      visible.push(selected);
    overflow = conditions.filter((b) => !visible.includes(b));
  }
  const branchEdits = editable && !item.readOnlyReason ? actions.edit : null;
  const conditionEditable =
    Boolean(branchEdits) && selected.kind !== "otherwise";
  const select = (branch: DocBranch) => {
    // Clicking the open branch again goes to its condition line.
    if (branch === selected && conditionEditable) actions.setFocusId(node.id);
    actions.selectBranch(node.id, branch.branchId);
  };
  const empty = selected.children.length === 0 && !selected.jumpTo;
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const conditionIndex = conditions.indexOf(selected);
  const canMoveEarlier = conditionIndex > 0;
  const canMoveLater =
    conditionIndex >= 0 && conditionIndex < conditions.length - 1;
  const canRemove = conditions.length > 1;
  const hasBranchActions = canMoveEarlier || canMoveLater || canRemove;
  // Steps outside the branch that use outputs of steps inside it.
  const outsideUsers = () => {
    const inside = new Set(selectedStepIds(selected.children));
    const users = [...inside].flatMap((id) => actions.outputUsers(id));
    return users.filter(
      (u, i) =>
        !inside.has(u.id) && users.findIndex((x) => x.id === u.id) === i,
    );
  };
  const removeSelected = () => {
    if (!branchEdits) return;
    if (outsideUsers().length) setConfirmingRemove(true);
    else branchEdits.removeBranch(node.id, selected.branchId);
  };
  return (
    <div className="rounded py-[3px]">
      {/* The hover group is the header only: the steps inside a branch show their own controls. */}
      <div className="group/line relative flex min-h-[26px] flex-wrap items-center gap-2">
        <DocTag node={node} place={editable ? place : undefined} />
        <button
          type="button"
          aria-label={`${open ? "Collapse" : "Expand"} ${docStepTitle(node)}`}
          aria-expanded={open}
          onClick={() => actions.toggleConditional(node.id)}
          className="grid size-[22px] place-items-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {open ? (
            <ChevronDownIcon className="size-3.5" />
          ) : (
            <ChevronRightIcon className="size-3.5" />
          )}
        </button>
        <span className="text-[12.5px] text-muted-foreground">
          {conditions.length} condition{conditions.length === 1 ? "" : "s"} +
          Otherwise · checked left to right
        </span>
        <DocStepControls
          node={node}
          editable={editable}
          showRunControl={showRunControl}
        />
      </div>
      {open && (
        <>
          <DocStepResult
            label={node.data.label}
            completedText={
              ranBranch
                ? `Took ${ranBranch.kind === "otherwise" ? "Otherwise" : `${kindLabel[ranBranch.kind]} ${shortLabel(ranBranch.condition)}`.trim()}`
                : undefined
            }
          />
          {typeof node.data.description === "string" &&
            node.data.description.trim() && (
              <p className="mb-1.5 text-base font-medium leading-[1.55]">
                <DocText value={node.data.description} />
              </p>
            )}
          <div
            role="tablist"
            aria-label={`Branches of ${node.data.label}`}
            className="mb-1 mt-1 flex flex-wrap items-center gap-x-3 gap-y-1"
          >
            {visible.map((branch) => (
              <BranchTab
                key={branch.branchId}
                branch={branch}
                selected={branch === selected}
                ran={branch === ranBranch}
                onSelect={() => select(branch)}
              />
            ))}
            {overflow.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-dashed border-border px-2 py-[3px] text-[12.5px] text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {overflow.length} more
                    <ChevronDownIcon className="size-3" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-w-[320px]">
                  {overflow.map((branch) => (
                    <DropdownMenuItem
                      key={branch.branchId}
                      onSelect={() => select(branch)}
                      className="gap-1.5 text-[13px]"
                    >
                      <span className="font-semibold text-[#8456A8] dark:text-[#B793DA]">
                        {kindLabel[branch.kind]}
                      </span>
                      <span className="min-w-0 truncate">
                        {shortLabel(branch.condition)}
                      </span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {otherwise && (
              <BranchTab
                branch={otherwise}
                selected={otherwise === selected}
                ran={otherwise === ranBranch}
                onSelect={() => select(otherwise)}
              />
            )}
            {branchEdits && (
              <>
                {selected.kind !== "otherwise" && hasBranchActions && (
                  <DocDeleteConfirm
                    id={confirmingRemove ? selected.branchId : null}
                    onClose={() => setConfirmingRemove(false)}
                    title="Remove this branch?"
                    confirmLabel="Remove"
                    users={confirmingRemove ? outsideUsers() : []}
                    remove={() =>
                      branchEdits.removeBranch(node.id, selected.branchId)
                    }
                  >
                    <span className="inline-flex">
                      <DropdownMenu modal={false}>
                        <DropdownMenuTrigger asChild>
                          <button
                            type="button"
                            aria-label={`Branch actions for ${kindLabel[selected.kind]} ${shortLabel(selected.condition)}`}
                            className="grid size-[26px] place-items-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            <DotsHorizontalIcon className="size-3.5" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="start">
                          {canMoveEarlier && (
                            <DropdownMenuItem
                              onSelect={() =>
                                branchEdits.moveBranch(
                                  node.id,
                                  selected.branchId,
                                  -1,
                                )
                              }
                            >
                              Move earlier
                            </DropdownMenuItem>
                          )}
                          {canMoveLater && (
                            <DropdownMenuItem
                              onSelect={() =>
                                branchEdits.moveBranch(
                                  node.id,
                                  selected.branchId,
                                  1,
                                )
                              }
                            >
                              Move later
                            </DropdownMenuItem>
                          )}
                          {canRemove && (
                            <DropdownMenuItem
                              className="text-destructive focus:text-destructive"
                              onSelect={() => setTimeout(removeSelected, 0)}
                            >
                              Remove
                            </DropdownMenuItem>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </span>
                  </DocDeleteConfirm>
                )}
                <button
                  type="button"
                  onClick={() => branchEdits.addBranch(node.id)}
                  className="rounded-md px-1 py-[3px] text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                >
                  + Branch
                </button>
              </>
            )}
          </div>
          <div role="tabpanel" data-doc-branch-id={selected.branchId}>
            <p className="text-base font-medium leading-[1.55]">
              {selected.kind === "otherwise" ? (
                "Otherwise"
              ) : (
                <>
                  {kindLabel[selected.kind]}{" "}
                  {conditionEditable && branchEdits ? (
                    <span className="inline-block min-w-[12ch] align-baseline">
                      <DocEditableLine
                        key={selected.branchId}
                        value={selected.condition}
                        editable
                        autoFocus={actions.focusId === node.id}
                        placeholder="write the condition, or use @"
                        label={`${node.data.label} condition`}
                        references={() =>
                          branchEdits.references(node.id, false)
                        }
                        onChange={(value) =>
                          branchEdits.setCondition(
                            node.id,
                            selected.branchId,
                            value,
                          )
                        }
                        onEnter={onEnter}
                        onRunStep={() => runStepByKey(node.id)}
                        onSettings={() => actions.openSettings(node.id)}
                        onDone={() => {
                          if (actions.focusId === node.id)
                            actions.setFocusId(null);
                        }}
                      />
                    </span>
                  ) : selected.condition ? (
                    <DocText value={selected.condition} />
                  ) : (
                    <span className="font-normal italic text-[#B45B0A] dark:text-[#E79A55]">
                      no condition
                    </span>
                  )}
                </>
              )}
            </p>
            <div className="my-0.5 flex items-center gap-[5px] text-xs leading-[1.55] text-muted-foreground">
              {selected.kind === "otherwise" ? (
                "Runs when no condition before it matches."
              ) : selected.check === "exact" ? (
                <>
                  <CodeIcon className="size-[11px]" />
                  Exact check, no AI
                </>
              ) : selected.check === "ai" ? (
                <>
                  <MagicWandIcon className="size-[11px]" />
                  Checked by AI
                </>
              ) : null}
            </div>
            <div className="mb-1 ml-[11px] mt-0.5 border-l border-border pl-4">
              {ranBranch &&
                selected !== ranBranch &&
                !selectedRan &&
                selected.stepCount > 0 && (
                  <p
                    className="my-1 text-[13px] text-muted-foreground"
                    data-testid="doc-branch-skipped"
                  >
                    {selected.kind === "otherwise"
                      ? "Not taken, so these steps were skipped"
                      : "No, so these steps were skipped"}
                  </p>
                )}
              {renderChildren(
                selected.children,
                selected.synthetic || selected.jumpTo
                  ? undefined
                  : {
                      parentId: node.id,
                      conditionalBranchId: selected.branchId,
                    },
                selected.branchId,
              )}
              {selected.jumpTo && (
                <DocJump
                  targetId={selected.jumpTo}
                  byId={byId}
                  verb="Then continue at"
                />
              )}
              {empty && (branchEdits ? selected.synthetic : true) && (
                <p className="my-1 text-[13px] text-muted-foreground">
                  {selected.kind === "otherwise"
                    ? "No steps. Goes straight to the step after the conditional."
                    : "No steps in this branch."}
                </p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
