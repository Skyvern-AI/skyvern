import { useContext, type ReactNode } from "react";
import {
  ArrowRightIcon,
  ChevronDownIcon,
  CodeIcon,
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
import type { AppNode, WorkflowBlockNode } from "../nodes";
import type { DocBranch, DocItem } from "./buildDocTree";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { docStepTitle } from "./docStepRegistry";
import { DocSettingsButton, DocTag } from "./DocStep";
import { DocText } from "./DocText";

const MAX_VISIBLE_CONDITIONS = 3;
const kindLabel = { if: "If", elseIf: "Else if", otherwise: "Otherwise" };

const shortLabel = (condition: string) => {
  const plain = stripJinjaReferences(condition).trim();
  return plain.length > 30 ? `${plain.slice(0, 29)}…` : plain;
};

function BranchTab({
  branch,
  selected,
  onSelect,
}: {
  branch: DocBranch;
  selected: boolean;
  onSelect: () => void;
}) {
  const label = branch.kind === "otherwise" ? "" : shortLabel(branch.condition);
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected}
      onClick={onSelect}
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
  renderChildren,
}: {
  node: WorkflowBlockNode;
  item: Extract<DocItem, { kind: "conditional" }>;
  byId: Map<string, AppNode>;
  editable: boolean;
  renderChildren: (children: DocItem[]) => ReactNode;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const conditions = item.branches.filter((b) => b.kind !== "otherwise");
  const otherwise = item.branches.find((b) => b.kind === "otherwise");
  const selectedId = actions.selectedBranch(node.id);
  const selected =
    item.branches.find((b) => b.branchId === selectedId) ?? item.branches[0]!;
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
  return (
    <div className="group/line my-0.5 rounded-lg border border-border pb-2 pl-3 pr-2.5 pt-1.5">
      <div className="mb-1.5 mt-0.5 flex min-h-[26px] flex-wrap items-center gap-2">
        <DocTag node={node} />
        <span className="text-[12.5px] text-muted-foreground">
          {conditions.length} condition{conditions.length === 1 ? "" : "s"} +
          Otherwise · checked left to right
        </span>
        <span className="ml-auto">
          <DocSettingsButton node={node} editable={editable} />
        </span>
      </div>
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
            onSelect={() => select(otherwise)}
          />
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
          {renderChildren(selected.children)}
          {selected.jumpTo && (
            <DocJump
              targetId={selected.jumpTo}
              byId={byId}
              verb="Then continue at"
            />
          )}
          {empty && (
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
