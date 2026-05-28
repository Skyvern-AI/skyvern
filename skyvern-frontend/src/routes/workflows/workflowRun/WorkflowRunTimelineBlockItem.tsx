import {
  CheckCircledIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CrossCircledIcon,
  CursorArrowIcon,
  Cross2Icon,
  DoubleArrowDownIcon,
  DropdownMenuIcon,
  FileTextIcon,
  HandIcon,
  InputIcon,
  KeyboardIcon,
  MagicWandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  type ActionsApiResponse,
  type ActionType,
  ActionTypes,
  ReadableActionTypes,
  Status,
} from "@/api/types";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import { cn } from "@/util/utils";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import {
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ThoughtCard } from "./ThoughtCard";
import { aggregateIterationStatus } from "./workflowTimelineUtils";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  block: WorkflowRunBlock;
  subItems: Array<WorkflowRunTimelineItem>;
  depth?: number;
  blockOrder?: ReadonlyMap<string, number>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

type LoopIterationGroup = {
  index: number | null;
  currentValue: string | null;
  items: Array<WorkflowRunTimelineItem>;
};

const INDENT_PX = 14;
const RAIL_HIGHLIGHT_OFFSET_PX = INDENT_PX / 2;
const RAIL_CONTENT_PADDING_PX = INDENT_PX - 1;

const railHighlightStyle = {
  marginLeft: `-${RAIL_HIGHLIGHT_OFFSET_PX}px`,
  paddingLeft: `${RAIL_CONTENT_PADDING_PX}px`,
};

const timelineActionIcons: Record<ActionType, React.ReactNode> = {
  [ActionTypes.Click]: (
    <WorkflowBlockIcon workflowBlockType="action" className="size-3.5" />
  ),
  [ActionTypes.Hover]: <HandIcon className="size-3.5" />,
  [ActionTypes.InputText]: <InputIcon className="size-3.5" />,
  [ActionTypes.DownloadFile]: (
    <WorkflowBlockIcon workflowBlockType="file_download" className="size-3.5" />
  ),
  [ActionTypes.UploadFile]: (
    <WorkflowBlockIcon workflowBlockType="file_upload" className="size-3.5" />
  ),
  [ActionTypes.SelectOption]: <DropdownMenuIcon className="size-3.5" />,
  [ActionTypes.complete]: <CheckCircledIcon className="size-3.5" />,
  [ActionTypes.wait]: (
    <WorkflowBlockIcon workflowBlockType="wait" className="size-3.5" />
  ),
  [ActionTypes.terminate]: <CrossCircledIcon className="size-3.5" />,
  [ActionTypes.SolveCaptcha]: <MagicWandIcon className="size-3.5" />,
  [ActionTypes.extract]: (
    <WorkflowBlockIcon workflowBlockType="extraction" className="size-3.5" />
  ),
  [ActionTypes.ReloadPage]: <ReloadIcon className="size-3.5" />,
  [ActionTypes.Scroll]: <DoubleArrowDownIcon className="size-3.5" />,
  [ActionTypes.KeyPress]: <KeyboardIcon className="size-3.5" />,
  [ActionTypes.Move]: <CursorArrowIcon className="size-3.5" />,
  [ActionTypes.NullAction]: <FileTextIcon className="size-3.5" />,
  [ActionTypes.VerificationCode]: <KeyboardIcon className="size-3.5" />,
  [ActionTypes.Drag]: <HandIcon className="size-3.5" />,
  [ActionTypes.LeftMouse]: (
    <WorkflowBlockIcon workflowBlockType="action" className="size-3.5" />
  ),
  [ActionTypes.GotoUrl]: (
    <WorkflowBlockIcon workflowBlockType="goto_url" className="size-3.5" />
  ),
  [ActionTypes.ClosePage]: <Cross2Icon className="size-3.5" />,
};

function IndentRails({ depth }: { depth: number }) {
  // Render guide rails only for nested rows. Top-level rows should start with
  // content, not a phantom outer timeline rail.
  const rails = depth;
  return (
    <>
      {Array.from({ length: rails }).map((_, i) => (
        <div
          key={i}
          className="relative shrink-0 self-stretch"
          style={{ width: `${INDENT_PX}px` }}
        >
          <div className="absolute inset-y-0 left-1/2 w-px bg-slate-700" />
        </div>
      ))}
    </>
  );
}

function StatusDot({
  status,
  isFinalized,
}: {
  status: Status | null;
  isFinalized: boolean;
}) {
  const isCompleted = status === Status.Completed;
  const isFailure =
    status === Status.Failed ||
    status === Status.Terminated ||
    status === Status.TimedOut ||
    status === Status.Canceled;
  const isRunning = status === Status.Running && !isFinalized;

  if (isCompleted) {
    return <CheckCircledIcon className="size-3.5 shrink-0 text-success" />;
  }
  if (isFailure) {
    return <CrossCircledIcon className="size-3.5 shrink-0 text-destructive" />;
  }
  if (isRunning) {
    return (
      <ReloadIcon className="size-3.5 shrink-0 animate-spin text-sky-400" />
    );
  }
  return <div className="size-2 shrink-0 rounded-full bg-slate-600" />;
}

function normalizeInlineText(value: string | null | undefined): string | null {
  const normalized = value?.replace(/\s+/g, " ").trim();
  return normalized ? normalized : null;
}

function getActionSummary(action: ActionsApiResponse): string | null {
  return (
    normalizeInlineText(action.reasoning) ??
    normalizeInlineText(action.text) ??
    normalizeInlineText(action.response)
  );
}

function countSchemaFields(value: WorkflowRunBlock["data_schema"]): number {
  if (!value || typeof value !== "object" || Array.isArray(value)) return 0;
  const properties = "properties" in value ? value.properties : null;
  if (
    properties &&
    typeof properties === "object" &&
    !Array.isArray(properties)
  ) {
    return Object.keys(properties).length;
  }
  return Object.keys(value).length;
}

function getTimelineDescriptor(block: WorkflowRunBlock): string {
  const explicit =
    normalizeInlineText(block.description) ??
    normalizeInlineText(block.navigation_goal) ??
    normalizeInlineText(block.data_extraction_goal) ??
    normalizeInlineText(block.prompt) ??
    normalizeInlineText(block.instructions) ??
    normalizeInlineText(block.url);

  if (explicit) return explicit;

  if (block.block_type === "extraction") {
    const fieldCount = countSchemaFields(block.data_schema);
    if (fieldCount > 0) {
      return `Extract ${fieldCount} ${fieldCount === 1 ? "field" : "fields"}`;
    }
  }

  if (block.block_type === "for_loop") {
    const valueCount = Array.isArray(block.loop_values)
      ? block.loop_values.length
      : 0;
    return valueCount > 0
      ? `Loop over ${valueCount} ${valueCount === 1 ? "value" : "values"}`
      : "Loop over values";
  }

  if (block.block_type === "while_loop") {
    return "Repeat while condition passes";
  }

  if (block.block_type === "conditional") {
    const expression = normalizeInlineText(block.executed_branch_expression);
    return expression ? `Branch on ${expression}` : "Branch on a condition";
  }

  return `${workflowBlockTitle[block.block_type]} block`;
}

function getTimelineTypeLabel(block: WorkflowRunBlock): string {
  switch (block.block_type) {
    case "conditional":
      return "Condition";
    case "for_loop":
    case "while_loop":
      return "Loop";
    case "navigation":
    case "task":
    case "task_v2":
      return "Task";
    case "http_request":
      return "HTTP";
    default:
      return workflowBlockTitle[block.block_type];
  }
}

function getLoopIterationGroups(
  items: Array<WorkflowRunTimelineItem>,
): Array<LoopIterationGroup> {
  const groupsByKey = new Map<string, LoopIterationGroup>();
  const unknownItems: Array<WorkflowRunTimelineItem> = [];

  items.forEach((item) => {
    const currentIndex = isBlockItem(item) ? item.block.current_index : null;
    const currentValue = isBlockItem(item) ? item.block.current_value : null;

    if (currentIndex === null) {
      unknownItems.push(item);
      return;
    }

    const groupKey = `index-${currentIndex}`;
    if (!groupsByKey.has(groupKey)) {
      groupsByKey.set(groupKey, {
        index: currentIndex,
        currentValue,
        items: [],
      });
    }
    const group = groupsByKey.get(groupKey)!;
    if (!group.currentValue && currentValue) {
      group.currentValue = currentValue;
    }
    group.items.push(item);
  });

  if (unknownItems.length > 0) {
    if (groupsByKey.size > 0) {
      let maxIndex = -1;
      let maxGroup: LoopIterationGroup | null = null;
      for (const group of groupsByKey.values()) {
        if (group.index !== null && group.index > maxIndex) {
          maxIndex = group.index;
          maxGroup = group;
        }
      }
      if (maxGroup) {
        unknownItems.forEach((item) => maxGroup!.items.push(item));
      }
    } else {
      groupsByKey.set("index-0", {
        index: 0,
        currentValue: null,
        items: unknownItems,
      });
    }
  }

  return Array.from(groupsByKey.values()).sort((left, right) => {
    if (left.index === null && right.index === null) return 0;
    if (left.index === null) return -1;
    if (right.index === null) return 1;
    return left.index - right.index;
  });
}

function timelineItemsContainActiveElement(
  items: Array<WorkflowRunTimelineItem>,
  activeItem: WorkflowRunOverviewActiveElement,
): boolean {
  if (activeItem === null || activeItem === "stream") return false;

  const stack = [...items];
  while (stack.length > 0) {
    const item = stack.pop()!;

    if (
      isBlockItem(item) &&
      isWorkflowRunBlock(activeItem) &&
      item.block.workflow_run_block_id === activeItem.workflow_run_block_id
    ) {
      return true;
    }
    if (
      isBlockItem(item) &&
      isAction(activeItem) &&
      item.block.actions?.some((a) => a.action_id === activeItem.action_id)
    ) {
      return true;
    }
    if (
      isThoughtItem(item) &&
      isObserverThought(activeItem) &&
      item.thought.thought_id === activeItem.thought_id
    ) {
      return true;
    }
    stack.push(...item.children);
  }
  return false;
}

type TimelineSubItemsProps = {
  items: Array<WorkflowRunTimelineItem>;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function TimelineSubItems({
  items,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: TimelineSubItemsProps) {
  return (
    <div>
      {items.map((item) => {
        if (isBlockItem(item)) {
          return (
            <WorkflowRunTimelineBlockItem
              key={item.block.workflow_run_block_id}
              subItems={item.children}
              activeItem={activeItem}
              activeIteration={activeIteration}
              block={item.block}
              depth={depth}
              blockOrder={blockOrder}
              onActionClick={onActionClick}
              onBlockItemClick={onBlockItemClick}
              onIterationClick={onIterationClick}
              onThoughtClick={onThoughtClick}
              renderThoughts={renderThoughts}
              finallyBlockLabel={finallyBlockLabel}
              workflowRunIsFinalized={workflowRunIsFinalized}
            />
          );
        }
        if (renderThoughts && isThoughtItem(item) && onThoughtClick) {
          return (
            <div key={item.thought.thought_id} className="py-1 pl-7">
              <ThoughtCard
                active={
                  isObserverThought(activeItem) &&
                  activeItem.thought_id === item.thought.thought_id
                }
                onClick={onThoughtClick}
                thought={item.thought}
              />
            </div>
          );
        }
        // Thoughts are no longer rendered as cards in the compact timeline
        // rail; the block detail panel surfaces them inside the owning
        // block's view alongside its actions.
        return null;
      })}
    </div>
  );
}

type TimelineActionRowsProps = {
  block: WorkflowRunBlock;
  activeItem: WorkflowRunOverviewActiveElement;
  depth: number;
  onActionClick: (action: ActionItem) => void;
  workflowRunIsFinalized?: boolean;
};

function TimelineActionRows({
  block,
  activeItem,
  depth,
  onActionClick,
  workflowRunIsFinalized,
}: TimelineActionRowsProps) {
  const actions = block.actions ?? [];
  const actionsTopDown = [...actions].reverse();

  if (actions.length === 0) return null;

  return (
    <div className="space-y-1 py-1">
      {actionsTopDown.map((action, index) => {
        const isActive =
          isAction(activeItem) && activeItem.action_id === action.action_id;
        const displayIndex = index + 1;
        const icon = timelineActionIcons[action.action_type];
        const label = ReadableActionTypes[action.action_type];
        const summary = getActionSummary(action);

        return (
          <div
            key={action.action_id}
            className="flex min-h-[24px] items-stretch text-xs"
          >
            <IndentRails depth={depth} />
            <div
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
                "hover:bg-slate-800/60",
                isActive && "bg-slate-800",
              )}
              style={railHighlightStyle}
            >
              <div className="size-4 shrink-0" />
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onActionClick({ block, action });
                }}
                aria-pressed={isActive}
                className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40"
              >
                <StatusDot
                  status={action.status}
                  isFinalized={!!workflowRunIsFinalized}
                />
                <span className="shrink-0 text-slate-400" aria-hidden="true">
                  {icon}
                </span>
                <span className="w-7 shrink-0 text-[10px] tabular-nums text-slate-500">
                  #{displayIndex}
                </span>
                <span className="shrink-0 rounded border border-slate-700 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">
                  {label}
                </span>
                {summary && (
                  <span className="min-w-0 flex-1 truncate text-slate-500">
                    · {summary}
                  </span>
                )}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function WorkflowRunTimelineBlockItem({
  activeItem,
  activeIteration = null,
  block,
  blockOrder,
  subItems,
  depth = 0,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized = false,
}: Props) {
  const isFinallyBlock = finallyBlockLabel && block.label === finallyBlockLabel;
  const isRunning = block.status === Status.Running && !workflowRunIsFinalized;
  const duration =
    block.duration !== null ? formatDuration(toDuration(block.duration)) : null;
  const blockTypeTitle = workflowBlockTitle[block.block_type];
  const blockTypeLabel = getTimelineTypeLabel(block);
  const blockName = block.label ?? block.title ?? blockTypeTitle;
  const blockIndex = blockOrder?.get(block.workflow_run_block_id);
  const descriptor = getTimelineDescriptor(block);
  const actions = block.actions ?? [];
  const actionCount = actions.length;

  const hasActions = actionCount > 0;
  const isForLoopBlock = block.block_type === "for_loop";
  const isWhileLoopBlock = block.block_type === "while_loop";
  const isLoopBlock = isForLoopBlock || isWhileLoopBlock;
  const loopIterationGroups = useMemo(
    () => (isLoopBlock ? getLoopIterationGroups(subItems) : []),
    [isLoopBlock, subItems],
  );
  const hasRenderableNestedChildren = subItems.some(
    (item) => isBlockItem(item) || (renderThoughts && isThoughtItem(item)),
  );
  // Only treat as a container when there are actual children to reveal.
  // Conditionals and loops can be defined as containers structurally, but if
  // the runtime didn't model any child blocks under them (e.g. conditionals
  // whose "next" block is a flat sibling), showing a chevron that reveals
  // nothing is worse than no chevron at all.
  const isContainer = hasRenderableNestedChildren || hasActions;

  // The loop block itself is only "active" when no specific iteration is
  // selected — otherwise the iteration row owns the highlight.
  //
  // When an action inside this block is the selection, the owning block stays
  // highlighted — mirrors the loop-iteration pattern so the user never loses
  // the parent context after drilling into Panel B's action cards.
  const ownsSelectedAction =
    isAction(activeItem) &&
    (block.actions ?? []).some((a) => a.action_id === activeItem.action_id);
  const hasResolvedActiveIteration =
    activeIteration !== null &&
    loopIterationGroups.some((group) => group.index === activeIteration);
  const isActiveBlock =
    ((isWorkflowRunBlock(activeItem) &&
      activeItem.workflow_run_block_id === block.workflow_run_block_id) ||
      ownsSelectedAction) &&
    !(isLoopBlock && hasResolvedActiveIteration);
  const hasActiveDescendant = useMemo(
    () => timelineItemsContainActiveElement(subItems, activeItem),
    [subItems, activeItem],
  );
  // Deep-link case: `?active=<loopId>&iteration=N`. The loop block is the
  // selected item, but `isActiveBlock` is intentionally suppressed (the
  // iteration row owns the highlight) and `hasActiveDescendant` is false
  // (the loop isn't its own child). Without this, the loop stays collapsed
  // and the targeted iteration row is hidden.
  const isLoopWithSelectedIteration =
    isLoopBlock &&
    hasResolvedActiveIteration &&
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === block.workflow_run_block_id;

  const [expanded, setExpanded] = useState(
    isRunning ||
      isActiveBlock ||
      hasActions ||
      hasActiveDescendant ||
      isLoopWithSelectedIteration ||
      !hasRenderableNestedChildren,
  );
  const userToggledRef = useRef(false);

  useEffect(() => {
    userToggledRef.current = false;
  }, [block.workflow_run_block_id]);

  // Auto-expand when actions appear, an active descendant appears, or this
  // block starts running.
  // Skip once the user has explicitly toggled the chevron — their choice wins.
  useEffect(() => {
    if (userToggledRef.current) return;
    if (
      hasActions ||
      hasActiveDescendant ||
      isRunning ||
      isLoopWithSelectedIteration
    ) {
      setExpanded(true);
    }
  }, [hasActions, hasActiveDescendant, isRunning, isLoopWithSelectedIteration]);

  const loopValues = Array.isArray(block.loop_values) ? block.loop_values : [];

  // Loop inline counter (e.g. 3/8).
  const loopCounter = isForLoopBlock
    ? loopValues.length > 0
      ? `${loopIterationGroups.length}/${loopValues.length}`
      : null
    : isWhileLoopBlock
      ? `${loopIterationGroups.length}`
      : null;

  return (
    <div className="min-w-0">
      <div className="flex min-h-[28px] items-stretch text-xs">
        <IndentRails depth={depth} />
        <div
          className={cn(
            "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-1 pr-1.5",
            "hover:bg-slate-800/60",
            isActiveBlock && "bg-slate-800",
          )}
          style={railHighlightStyle}
        >
          {isContainer ? (
            <button
              type="button"
              className="inline-flex size-4 shrink-0 items-center justify-center rounded text-slate-400 outline-none hover:bg-slate-700 hover:text-slate-200 focus-visible:ring-1 focus-visible:ring-white/40"
              onClick={(event) => {
                event.stopPropagation();
                userToggledRef.current = true;
                setExpanded((prev) => !prev);
              }}
              aria-label={expanded ? "Collapse" : "Expand"}
              aria-expanded={expanded}
            >
              {expanded ? (
                <ChevronDownIcon className="size-4" />
              ) : (
                <ChevronRightIcon className="size-4" />
              )}
            </button>
          ) : (
            <div className="size-4 shrink-0" />
          )}
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onBlockItemClick(block);
            }}
            aria-pressed={isActiveBlock}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40"
          >
            <StatusDot
              status={block.status}
              isFinalized={!!workflowRunIsFinalized}
            />
            <span title={blockTypeTitle} className="shrink-0">
              <WorkflowBlockIcon
                workflowBlockType={block.block_type}
                className="size-3.5 text-slate-300"
              />
            </span>
            {blockIndex !== undefined && (
              <span className="w-7 shrink-0 text-[10px] tabular-nums text-slate-500">
                #{blockIndex}
              </span>
            )}
            <span className="inline-flex min-w-[6rem] max-w-[8rem] shrink-0 justify-center truncate rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] font-medium text-slate-300">
              {blockTypeLabel}
            </span>
            <span className="min-w-0 max-w-[12rem] truncate text-slate-200">
              {blockName}
            </span>
            <span className="min-w-0 flex-1 truncate text-slate-500">
              · {descriptor}
            </span>
            {isFinallyBlock && (
              <span className="shrink-0 rounded bg-amber-500/80 px-1 text-[9px] font-medium text-black">
                finally
              </span>
            )}
            {hasActions && (
              <span className="shrink-0 rounded bg-slate-800 px-1 text-[10px] tabular-nums text-slate-400">
                {actionCount} {actionCount === 1 ? "action" : "actions"}
              </span>
            )}
            {loopCounter && (
              <span className="shrink-0 rounded bg-slate-700 px-1 text-[10px] tabular-nums text-slate-300">
                {loopCounter}
              </span>
            )}
            {duration && (
              <span className="shrink-0 text-[10px] tabular-nums text-slate-500">
                {duration}
              </span>
            )}
          </button>
        </div>
      </div>

      {/* Container body — always mounted so open/close transitions animate */}
      {isContainer && (
        <Collapsible open={expanded}>
          <CollapsibleContent className="motion-safe:data-[state=closed]:animate-collapsible-up-fade motion-safe:data-[state=open]:animate-collapsible-down-fade overflow-hidden">
            {hasActions && (
              <TimelineActionRows
                block={block}
                activeItem={activeItem}
                depth={depth + 1}
                onActionClick={onActionClick}
                workflowRunIsFinalized={workflowRunIsFinalized}
              />
            )}
            {isLoopBlock && loopIterationGroups.length > 0 ? (
              <LoopIterationRows
                loopBlock={block}
                groups={loopIterationGroups}
                activeItem={activeItem}
                activeIteration={activeIteration}
                depth={depth + 1}
                blockOrder={blockOrder}
                onBlockItemClick={onBlockItemClick}
                onIterationClick={onIterationClick}
                onActionClick={onActionClick}
                onThoughtClick={onThoughtClick}
                renderThoughts={renderThoughts}
                finallyBlockLabel={finallyBlockLabel}
                workflowRunIsFinalized={workflowRunIsFinalized}
              />
            ) : (
              hasRenderableNestedChildren && (
                <TimelineSubItems
                  items={subItems}
                  activeItem={activeItem}
                  activeIteration={activeIteration}
                  depth={depth + 1}
                  blockOrder={blockOrder}
                  onActionClick={onActionClick}
                  onBlockItemClick={onBlockItemClick}
                  onIterationClick={onIterationClick}
                  onThoughtClick={onThoughtClick}
                  renderThoughts={renderThoughts}
                  finallyBlockLabel={finallyBlockLabel}
                  workflowRunIsFinalized={workflowRunIsFinalized}
                />
              )
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}

type LoopIterationRowsProps = {
  loopBlock: WorkflowRunBlock;
  groups: Array<LoopIterationGroup>;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function LoopIterationRows({
  loopBlock,
  groups,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: LoopIterationRowsProps) {
  return (
    <div>
      {groups.map((group, groupIndex) => (
        // Key by group.index alone so existing iteration rows keep their
        // identity (and useState) when a new iteration arrives at the top
        // of the DESC-sorted list. Including groupIndex would shift every
        // existing row's key and remount the whole stack on each update.
        <LoopIterationRow
          key={group.index ?? "unknown"}
          loopBlock={loopBlock}
          group={group}
          groupIndex={groupIndex}
          groupCount={groups.length}
          activeItem={activeItem}
          activeIteration={activeIteration}
          depth={depth}
          blockOrder={blockOrder}
          onBlockItemClick={onBlockItemClick}
          onIterationClick={onIterationClick}
          onActionClick={onActionClick}
          onThoughtClick={onThoughtClick}
          renderThoughts={renderThoughts}
          finallyBlockLabel={finallyBlockLabel}
          workflowRunIsFinalized={workflowRunIsFinalized}
        />
      ))}
    </div>
  );
}

type LoopIterationRowProps = {
  loopBlock: WorkflowRunBlock;
  group: LoopIterationGroup;
  groupIndex: number;
  groupCount: number;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function LoopIterationRow({
  loopBlock,
  group,
  groupIndex,
  groupCount,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: LoopIterationRowProps) {
  const hasActiveDescendant = useMemo(
    () => timelineItemsContainActiveElement(group.items, activeItem),
    [group.items, activeItem],
  );
  const status = useMemo(
    () => aggregateIterationStatus(group.items),
    [group.items],
  );

  // Default open: latest group, running, or has active.
  const [expanded, setExpanded] = useState(
    groupIndex === groupCount - 1 ||
      hasActiveDescendant ||
      status === Status.Running,
  );
  const userToggledRef = useRef(false);

  useEffect(() => {
    userToggledRef.current = false;
  }, [loopBlock.workflow_run_block_id, group.index]);

  // Mirror the block-row pattern: auto-expand when status flips to running
  // or an active descendant appears, unless the user has explicitly
  // collapsed this row.
  useEffect(() => {
    if (userToggledRef.current) return;
    if (hasActiveDescendant || status === Status.Running) {
      setExpanded(true);
    }
  }, [hasActiveDescendant, status]);

  const iterationIndex = group.index !== null ? group.index : groupIndex;
  const iterationNumber = iterationIndex + 1;
  const currentValuePreview = normalizeInlineText(group.currentValue);
  const isActiveIteration =
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === loopBlock.workflow_run_block_id &&
    activeIteration === iterationIndex;

  return (
    <div className="min-w-0">
      <div className="flex min-h-[24px] items-stretch text-[11px] text-slate-300">
        <IndentRails depth={depth} />
        <div
          className={cn(
            "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
            "hover:bg-slate-800/60",
            isActiveIteration && "bg-slate-800",
          )}
          style={railHighlightStyle}
        >
          <button
            type="button"
            className="-ml-0.5 size-3.5 shrink-0 rounded text-slate-500 outline-none hover:bg-slate-700 hover:text-slate-200 focus-visible:ring-1 focus-visible:ring-white/40"
            onClick={(event) => {
              event.stopPropagation();
              userToggledRef.current = true;
              setExpanded((prev) => !prev);
            }}
            aria-label={expanded ? "Collapse iteration" : "Expand iteration"}
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDownIcon className="size-3.5" />
            ) : (
              <ChevronRightIcon className="size-3.5" />
            )}
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              if (onIterationClick) {
                onIterationClick(loopBlock, iterationIndex);
              } else {
                setExpanded((prev) => !prev);
              }
            }}
            aria-pressed={isActiveIteration}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40"
          >
            <StatusDot status={status} isFinalized={!!workflowRunIsFinalized} />
            <span className="shrink-0 text-slate-400">
              Iteration {iterationNumber}
            </span>
            {currentValuePreview && (
              <span className="min-w-0 flex-1 truncate text-slate-500">
                · {currentValuePreview}
              </span>
            )}
          </button>
        </div>
      </div>
      <Collapsible open={expanded}>
        <CollapsibleContent className="motion-safe:data-[state=closed]:animate-collapsible-up-fade motion-safe:data-[state=open]:animate-collapsible-down-fade overflow-hidden">
          <TimelineSubItems
            items={group.items}
            activeItem={activeItem}
            activeIteration={activeIteration}
            depth={depth + 1}
            blockOrder={blockOrder}
            onActionClick={onActionClick}
            onBlockItemClick={onBlockItemClick}
            onIterationClick={onIterationClick}
            onThoughtClick={onThoughtClick}
            renderThoughts={renderThoughts}
            finallyBlockLabel={finallyBlockLabel}
            workflowRunIsFinalized={workflowRunIsFinalized}
          />
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
