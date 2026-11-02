import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
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
import { docStepCanRun, docStepTitle } from "./docStepRegistry";
import { DocRunStepButton } from "./DocRunStepButton";
import { DocStepResult } from "./DocStepResult";
import { DocSettingsButton, DocTag, type DocStepPlace } from "./DocStep";
import type { DocScopeRef } from "./docGraphEdits";
import { DocText } from "./DocText";
import { DocDeleteConfirm } from "./DocEditing";
import { useDocRun } from "./useDocRun";
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
        "flex min-w-0 max-w-[min(240px,100%)] items-center gap-1.5 whitespace-nowrap rounded-md px-2 py-[3px] text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
        selected &&
          "bg-[#F3EEF8] text-foreground hover:bg-[#F3EEF8] dark:bg-[rgba(183,147,218,.15)] dark:hover:bg-[rgba(183,147,218,.15)]",
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
  renderChildren,
}: {
  node: WorkflowBlockNode;
  item: Extract<DocItem, { kind: "conditional" }>;
  byId: Map<string, AppNode>;
  editable: boolean;
  showRunControl?: boolean;
  place?: DocStepPlace;
  renderChildren: (
    children: DocItem[],
    scope?: DocScopeRef,
    branchId?: string,
  ) => ReactNode;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const conditions = item.branches.filter((b) => b.kind !== "otherwise");
  const otherwise = item.branches.find((b) => b.kind === "otherwise");
  const { blocksByLabel } = useDocRun();
  const { selected, ranBranch } = docBranchSelection(
    item,
    node.data.label,
    actions.selectedBranch(node.id),
    byId,
    blocksByLabel,
  );
  let visible = conditions;
  let overflow: DocBranch[] = [];
  if (conditions.length > MAX_VISIBLE_CONDITIONS) {
    visible = conditions.slice(0, MAX_VISIBLE_CONDITIONS - 1);
    if (selected.kind !== "otherwise" && !visible.includes(selected))
      visible.push(selected);
    overflow = conditions.filter((b) => !visible.includes(b));
  }
  const select = (branch: DocBranch) =>
    actions.selectBranch(node.id, branch.branchId);
  const empty = selected.children.length === 0 && !selected.jumpTo;
  const branchEdits = editable && !item.readOnlyReason ? actions.edit : null;
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const conditionIndex = conditions.indexOf(selected);
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
    <div className="group/line relative my-0.5 rounded-lg border border-border pb-2 pl-3 pr-2.5 pt-1.5">
      <div
        className={cn(
          "mb-1.5 mt-0.5 flex min-h-[26px] flex-wrap items-center gap-2",
          editable && "pr-16",
        )}
      >
        <DocTag node={node} place={editable ? place : undefined} />
        <span className="text-[12.5px] text-muted-foreground">
          {conditions.length} condition{conditions.length === 1 ? "" : "s"} +
          Otherwise · checked left to right
        </span>
        {/* Pinned to the corner: in a narrow pane the hidden controls would otherwise wrap onto a blank line. */}
        <span className="absolute right-2.5 top-2 flex items-center gap-2">
          <DocSettingsButton
            node={node}
            editable={editable}
            inline={editable && docStepCanRun(node)}
          />
          {showRunControl && (
            <DocRunStepButton node={node} disabled={!editable} />
          )}
        </span>
      </div>
      <DocStepResult label={node.data.label} />
      {typeof node.data.description === "string" &&
        node.data.description.trim() && (
          <p className="mb-1.5 text-base font-medium leading-[1.55]">
            <DocText value={node.data.description} />
          </p>
        )}
      <div
        role="tablist"
        aria-label={`Branches of ${node.data.label}`}
        className="mb-1.5 flex flex-wrap gap-1 border-b border-border pb-1.5"
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
            {selected.kind !== "otherwise" && (
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
                      {conditionIndex > 0 && (
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
                      {conditionIndex < conditions.length - 1 && (
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
                      {conditions.length > 1 && (
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
              className="rounded-md px-2 py-[3px] text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
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
              {selected.condition ? (
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
          {renderChildren(
            selected.children,
            selected.synthetic || selected.jumpTo
              ? undefined
              : { parentId: node.id, conditionalBranchId: selected.branchId },
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
    </div>
  );
}
