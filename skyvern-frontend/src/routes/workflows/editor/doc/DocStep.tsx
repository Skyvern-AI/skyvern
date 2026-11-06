import {
  useContext,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useDocUnfinishedStore } from "./docUnfinished";
import { stripJinjaReferences } from "./docTextUtils";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { goalChangeIsPending } from "../workflowEditorUtils";
import type { CodeBlockNodeData } from "../nodes/CodeBlockNode/types";
import {
  CheckIcon,
  ChevronRightIcon,
  ClockIcon,
  Cross2Icon,
  GearIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/util/utils";
import { DocKeyLabel, DocKeyTooltip } from "./DocKeyLabel";
import type { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { announceDoc, announceMoved, announceTurnedInto } from "./docAnnounce";
import { workflowBlockTitle } from "../nodes/types";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { getDocSettingsChips } from "./docSettingsChips";
import {
  docBlockType,
  docTagClasses,
  docTagColor,
  getDocKeyFields,
  docStepProducesData,
  getDocMainPlaceholder,
  getDocMissingSettings,
  loopLineLeadsIn,
  getDocProseFields,
  getDocSettingSummary,
  getDocStepSummary,
} from "./docStepRegistry";
import { DocRunStepButton } from "./DocRunStepButton";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import { docTagState, type DocTagState } from "./docRunState";
import { useDocRun } from "./useDocRun";
import { DocText } from "./DocText";
import type { DocPlace } from "./docEditApi";
import {
  DocDeleteConfirm,
  DocEditableLine,
  DocMoveToDialog,
  DocOutputName,
  DocStepPicker,
} from "./DocEditing";
import { useDocStepPending } from "./DocProposalView";
import {
  docShortcut,
  takeCaretAtStart,
  type LineSplit,
  type PickerOpener,
} from "./docKeys";

/** Where a step sits, for the moves its tag menu offers. Absent when the step's place can't be edited. */
export type DocStepPlace = DocPlace & { count: number; isFinally: boolean };

const tagStateClasses: Partial<Record<DocTagState, string>> = {
  succeeded:
    "bg-[#EAF3EC] text-[#3F7A57] dark:bg-[rgba(108,191,140,.14)] dark:text-[#6CBF8C]",
  failed:
    "bg-[#FBEAE8] text-[#C0392B] dark:bg-[rgba(240,138,126,.15)] dark:text-[#F08A7E]",
  waiting:
    "bg-[#FBEEE2] text-[#B45B0A] dark:bg-[rgba(231,154,85,.15)] dark:text-[#E79A55]",
  // The dark block-type colours would outrank plain utilities, so dark restates the grey.
  notReached:
    "bg-muted text-muted-foreground dark:bg-[rgba(255,255,255,.04)] dark:text-muted-foreground",
};

const tagStateNames: Record<DocTagState, string> = {
  running: "running",
  succeeded: "done",
  failed: "failed",
  waiting: "waiting for you",
  notReached: "not reached",
};

const tagClass = (
  blockType: ReturnType<typeof docBlockType>,
  pending = false,
  state: DocTagState | null = null,
) =>
  cn(
    "inline-flex h-[21px] shrink-0 items-center gap-1 whitespace-nowrap rounded px-[6px] text-[12.5px] font-semibold leading-none",
    docTagClasses[docTagColor(blockType)],
    pending &&
      "border border-dashed border-current bg-transparent px-[5px] opacity-80 dark:bg-transparent",
    state && tagStateClasses[state],
  );

/** The step's state in the Doc's run, for its tag. */
function useDocTagState(label: string): DocTagState | null {
  const {
    runId,
    blocksByLabel,
    live,
    blockRun,
    unknownLabels,
    ranLabels,
    rowsLoaded,
  } = useDocRun();
  const block = blocksByLabel.get(label);
  if (unknownLabels.has(label) || !rowsLoaded) return null;
  if (blockRun && !block) return null;
  if (!block && ranLabels && !ranLabels.has(label)) return null;
  return docTagState(block, live, Boolean(runId));
}

function DocTagIcon({
  blockType,
  state,
}: {
  blockType: ReturnType<typeof docBlockType>;
  state: DocTagState | null;
}) {
  const icon = "size-3 shrink-0";
  if (state === "running")
    return <ReloadIcon aria-hidden className={cn(icon, "animate-spin")} />;
  if (state === "succeeded") return <CheckIcon aria-hidden className={icon} />;
  if (state === "failed") return <Cross2Icon aria-hidden className={icon} />;
  if (state === "waiting") return <ClockIcon aria-hidden className={icon} />;
  return blockType ? (
    <WorkflowBlockIcon workflowBlockType={blockType} className={icon} />
  ) : null;
}

// The six dots of the Visual editor's node grip.
function GripDots() {
  return (
    <svg
      width="8"
      height="14"
      viewBox="0 0 8 14"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="1.5" cy="1.5" r="1.5" />
      <circle cx="6.5" cy="1.5" r="1.5" />
      <circle cx="1.5" cy="7" r="1.5" />
      <circle cx="6.5" cy="7" r="1.5" />
      <circle cx="1.5" cy="12.5" r="1.5" />
      <circle cx="6.5" cy="12.5" r="1.5" />
    </svg>
  );
}

function DocStepMenu({
  node,
  place,
  besideChevron = false,
}: {
  node: WorkflowBlockNode;
  place: DocStepPlace;
  // A loop's collapse arrow sits in the margin, so the handle goes left of it.
  besideChevron?: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const edit = actions.edit!;
  const [moveOpen, setMoveOpen] = useState(false);
  // The menu opens under the grip or under the type label, whichever was clicked.
  const [menuAt, setMenuAt] = useState<"grip" | "tag" | null>(null);
  const tagRef = useRef<HTMLButtonElement>(null);
  const gripRef = useRef<HTMLButtonElement>(null);
  const [confirming, setConfirming] = useState(false);
  const blockType = docBlockType(node);
  const pending = useDocStepPending(node);
  const state = useDocTagState(node.data.label);
  const playing = useBlockPlayRegistry((s) => s.handles[node.id]?.playing);
  // A step that is running can't be moved, changed into another type or deleted.
  const running = state === "running" || Boolean(playing);
  // The grip unmounts while the step runs; its menu must not come back on its own afterwards.
  useEffect(() => {
    if (running) setMenuAt((open) => (open === "grip" ? null : open));
  }, [running]);
  const container = node.type === "loop" || node.type === "conditional";
  const topLevel = place.scope.parentId === null;
  const destinations = moveOpen
    ? actions
        .destinations()
        .filter(
          (d) =>
            d.insideOf !== node.id &&
            !(
              d.scope.parentId === place.scope.parentId &&
              d.scope.conditionalBranchId === place.scope.conditionalBranchId &&
              place.index === 0
            ),
        )
    : [];
  // The menu's trigger goes with the step, so focus moves to the step that takes its place.
  const refocus = () => {
    // `edit` still reads the graph from before the delete.
    const neighbour = edit.neighbourOf(node.id);
    requestAnimationFrame(() => actions.setFocusId(neighbour));
  };
  const remove = () => {
    if (actions.outputUsers(node.id).length) setConfirming(true);
    else if (edit.remove(node.id)) refocus();
  };
  const toggleFrom = (at: "grip" | "tag") =>
    setMenuAt((open) => (open === at ? null : at));
  const openOnArrow = (at: "grip" | "tag") => (event: KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setMenuAt(at);
    }
  };
  const menu = (at: "grip" | "tag") =>
    menuAt === at && (
      <DropdownMenu
        modal={false}
        open
        onOpenChange={(open) => !open && setMenuAt(null)}
      >
        <DropdownMenuTrigger asChild>
          <span
            aria-hidden
            tabIndex={-1}
            className="pointer-events-none absolute inset-0"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          className="w-56"
          onPointerDownOutside={(event) => {
            // A click on the grip or the label toggles the menu itself.
            const target = event.target as Element;
            if (
              tagRef.current?.contains(target) ||
              gripRef.current?.contains(target)
            )
              event.preventDefault();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (
              document.activeElement === document.body ||
              !document.activeElement
            )
              (at === "grip" ? gripRef : tagRef).current?.focus();
          }}
        >
          {!container && !running && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>Turn into…</DropdownMenuSubTrigger>
              <DropdownMenuPortal>
                <DropdownMenuSubContent
                  collisionPadding={8}
                  className="w-[min(320px,calc(100vw-16px))] min-w-0 max-w-[var(--radix-dropdown-menu-content-available-width)] p-0"
                >
                  <DocStepPicker
                    placeholder="Turn into…"
                    exclude={[node.type, "loop", "conditional"]}
                    onPick={(type) => {
                      setMenuAt(null);
                      setTimeout(() => {
                        edit.turnInto(node.id, type);
                        announceTurnedInto(type);
                      }, 0);
                    }}
                  />
                </DropdownMenuSubContent>
              </DropdownMenuPortal>
            </DropdownMenuSub>
          )}
          <DropdownMenuItem onSelect={() => actions.openSettings(node.id)}>
            Settings
          </DropdownMenuItem>
          {!place.isFinally && (
            <DropdownMenuItem
              onSelect={() =>
                setTimeout(() => {
                  edit.duplicate(node.id);
                  announceDoc(`Duplicated ${node.data.label}`);
                }, 0)
              }
            >
              Duplicate
            </DropdownMenuItem>
          )}
          {topLevel && !container && !running && (
            <DropdownMenuItem
              onSelect={() => edit.setFinally(node.id, !place.isFinally)}
            >
              {place.isFinally
                ? "Don't always run at the end"
                : "Always run at the end"}
            </DropdownMenuItem>
          )}
          {!container && !running && edit.canBePlainText(node.id) && (
            <DropdownMenuItem
              onSelect={() =>
                setTimeout(
                  () => actions.setPendingNote(edit.toPlainText(node.id)),
                  0,
                )
              }
            >
              Back to plain text
            </DropdownMenuItem>
          )}
          {!place.isFinally && !running && (
            <>
              <DropdownMenuSeparator />
              {place.index > 0 && (
                <DropdownMenuItem
                  onSelect={() => {
                    if (
                      edit.move(node.id, {
                        scope: place.scope,
                        index: place.index - 1,
                      })
                    )
                      announceMoved("up");
                  }}
                >
                  Move up
                </DropdownMenuItem>
              )}
              {place.index < place.count - 1 && (
                <DropdownMenuItem
                  onSelect={() => {
                    if (
                      edit.move(node.id, {
                        scope: place.scope,
                        index: place.index + 1,
                      })
                    )
                      announceMoved("down");
                  }}
                >
                  Move down
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onSelect={() => setTimeout(() => setMoveOpen(true), 0)}
              >
                Move to…
              </DropdownMenuItem>
            </>
          )}
          {!running && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="text-red-700 focus:text-red-700 dark:text-red-400 dark:focus:text-red-400"
                onSelect={() => setTimeout(remove, 0)}
              >
                Delete
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  return (
    <>
      {/* The grip sits in the margin: on hover or focus with a mouse, always on touch. Never while the step runs. */}
      {!running && (
        <span
          data-doc-step-handle
          className={cn(
            "absolute right-full top-px z-10 flex",
            // Below sm a loop's grip joins its header row after the arrow, so the arrow keeps main's place in the 32px margin.
            besideChevron
              ? "mr-7 max-sm:static max-sm:ml-1 max-sm:mr-0 sm:max-[719px]:mr-1 [@media(hover:none)_and_(min-width:640px)]:mr-1"
              : "mr-1",
            "opacity-0 focus-within:opacity-100 group-focus-within/line:opacity-100 group-hover/line:opacity-100 max-[719px]:opacity-100 [@media(hover:none)]:opacity-100",
            menuAt === "grip" && "opacity-100",
          )}
        >
          <span className="relative inline-flex">
            <button
              type="button"
              ref={gripRef}
              // The type label is the keyboard way in; the grip is for the pointer.
              tabIndex={-1}
              // A menu that opens on press would cancel the drag, so it opens on click.
              aria-haspopup="menu"
              aria-expanded={menuAt === "grip"}
              aria-label={`Actions for ${node.data.label}`}
              onClick={() => toggleFrom("grip")}
              draggable={!place.isFinally}
              onDragStart={(event) => {
                setMenuAt(null);
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", node.data.label);
                // The whole step follows the pointer, not just the grip.
                const line = event.currentTarget.closest("[data-doc-node-id]");
                if (line) event.dataTransfer.setDragImage(line, 0, 0);
                actions.setDragId(node.id);
              }}
              onDragEnd={() => actions.setDragId(null)}
              className={cn(
                // The background hides a parent loop's rule where the grip sits; the pseudo-element is the 36px touch target.
                "relative grid size-6 place-items-center rounded bg-background text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring [@media(hover:none)]:before:absolute [@media(hover:none)]:before:-inset-1.5 [@media(hover:none)]:before:content-['']",
                !place.isFinally &&
                  "cursor-grab active:cursor-grabbing [@media(hover:none)]:cursor-pointer",
              )}
            >
              <GripDots />
            </button>
            {menu("grip")}
          </span>
        </span>
      )}
      <DocDeleteConfirm
        id={confirming ? node.id : null}
        onClose={() => setConfirming(false)}
        onRemoved={refocus}
      >
        <span className="relative inline-flex">
          <button
            type="button"
            ref={tagRef}
            data-doc-tag
            aria-haspopup="menu"
            aria-expanded={menuAt === "tag"}
            onClick={() => toggleFrom("tag")}
            onKeyDown={openOnArrow("tag")}
            title="Click for actions"
            aria-label={`${blockType ? workflowBlockTitle[blockType] : node.type} step ${node.data.label}, actions`}
            data-doc-tag-state={state}
            className={cn(
              tagClass(blockType, pending, state),
              "outline-none hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <DocTagIcon blockType={blockType} state={state} />
            {blockType ? workflowBlockTitle[blockType] : node.type}
            {state && <span className="sr-only">, {tagStateNames[state]}</span>}
          </button>
          {menu("tag")}
        </span>
      </DocDeleteConfirm>
      <DocMoveToDialog
        open={moveOpen}
        onOpenChange={setMoveOpen}
        destinations={destinations}
        onPick={(d) => {
          if (edit.move(node.id, d)) announceMoved({ place: d.label });
        }}
      />
    </>
  );
}

export function DocTag({
  node,
  place,
  besideChevron,
}: {
  node: WorkflowBlockNode;
  place?: DocStepPlace;
  besideChevron?: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const blockType = docBlockType(node);
  const pending = useDocStepPending(node);
  const state = useDocTagState(node.data.label);
  if (actions.edit && place)
    return (
      <DocStepMenu node={node} place={place} besideChevron={besideChevron} />
    );
  return (
    <span
      className={tagClass(blockType, pending, state)}
      data-doc-tag-state={state}
    >
      <DocTagIcon blockType={blockType} state={state} />
      {blockType ? workflowBlockTitle[blockType] : node.type}
      {state && <span className="sr-only">, {tagStateNames[state]}</span>}
    </span>
  );
}

export function DocSettingsButton({
  node,
  editable,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  if (!editable) return null;
  const changed = getDocSettingsChips(node).length;
  // Without a changed count the gear is not rendered at rest (display: none), so nothing is reserved for it.
  return (
    <DocKeyTooltip
      content={
        <>
          <DocKeyLabel
            label={
              changed
                ? `${changed} setting${changed > 1 ? "s" : ""} changed`
                : "Settings"
            }
            keys="."
          />
        </>
      }
    >
      <button
        type="button"
        aria-label={`Settings for ${node.data.label}${changed ? `, ${changed} changed` : ""}`}
        aria-keyshortcuts={docShortcut(".").aria}
        onClick={() => actions.openSettings(node.id)}
        className={cn(
          "items-center gap-[3px] rounded-[5px] py-0.5 text-xs font-semibold leading-4 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
          changed
            ? "inline-flex px-1"
            : "hidden px-1 focus-visible:inline-flex group-focus-within/line:inline-flex group-hover/line:inline-flex [@media(hover:none)]:inline-flex",
        )}
      >
        <GearIcon className="size-[15px]" />
        {changed > 0 && <span>{changed}</span>}
      </button>
    </DocKeyTooltip>
  );
}

/** Run/stop then settings, as one group: a control that is absent leaves no gap, and the gear trails so Run never moves. */
export function DocStepControls({
  node,
  editable,
  showRunControl,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  showRunControl: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {showRunControl && <DocRunStepButton node={node} disabled={!editable} />}
      <DocSettingsButton node={node} editable={editable} />
    </span>
  );
}

export function DocStepHeader({
  node,
  editable,
  showRunControl = editable,
  extra,
  disclosure,
  place,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  showRunControl?: boolean;
  extra?: ReactNode;
  disclosure?: { open: boolean; onToggle: () => void };
  place?: DocStepPlace;
}) {
  return (
    <div className="relative flex min-h-[26px] flex-wrap items-center gap-2">
      {disclosure && (
        <button
          type="button"
          aria-expanded={disclosure.open}
          aria-label={`${disclosure.open ? "Collapse" : "Expand"} ${node.data.label}`}
          onClick={disclosure.onToggle}
          className={cn(
            "absolute -left-[26px] top-0.5 grid h-[22px] w-[22px] place-items-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
            // On touch the grip shows at rest, so from sm up the arrow moves out to its left: arrow, grip, label.
            editable &&
              place &&
              "sm:max-[719px]:-left-[54px] [@media(hover:none)_and_(min-width:640px)]:-left-[54px]",
          )}
        >
          <ChevronRightIcon
            className={cn(
              "size-4 transition-transform",
              disclosure.open && "rotate-90",
            )}
          />
        </button>
      )}
      <DocTag
        node={node}
        place={editable ? place : undefined}
        besideChevron={Boolean(disclosure)}
      />
      {extra}
      {docStepProducesData(node) && (
        <span className="flex min-w-0 items-baseline gap-1 text-[12.5px] text-muted-foreground">
          saves{" "}
          {editable && place ? (
            <DocOutputName id={node.id} label={node.data.label} />
          ) : (
            <span
              title={`${node.data.label}_output`}
              className="inline-block min-w-0 max-w-full rounded bg-muted px-[5px] align-baseline font-medium leading-[1.25] [overflow-wrap:anywhere]"
            >
              {node.data.label}_output
            </span>
          )}
        </span>
      )}
      <DocStepControls
        node={node}
        editable={editable}
        showRunControl={showRunControl}
      />
    </div>
  );
}

export function DocStepBody({
  node,
  editable: editableProp = false,
  onEnter,
  onSlash,
  onTab,
  onBackspaceAtStart,
  onMove,
  onRunStep,
}: {
  node: WorkflowBlockNode;
  editable?: boolean;
  // Enter; `key` is the field the caret was in, and the split is null outside the step's main text.
  onEnter?: (split: LineSplit | null, key: string) => void;
  onSlash?: PickerOpener;
  onTab?: (outdent: boolean) => boolean;
  onBackspaceAtStart?: () => boolean;
  onMove?: (delta: -1 | 1) => boolean;
  onRunStep?: () => void;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const data = node.data as Record<string, unknown>;
  // A build in flight carries the Goal it started with, so the Code line is read-only until it ends, as in Visual.
  const goalBuild = useCopilotActionStore((state) =>
    state.generatingBlockLabel === node.data.label
      ? "building"
      : state.queuedBuilds.some(
            (queued) => queued.blockLabel === node.data.label,
          )
        ? "queued"
        : null,
  );
  const editable = editableProp && !(node.type === "codeBlock" && goalBuild);
  const goalNotApplied =
    node.type === "codeBlock" &&
    !goalBuild &&
    goalChangeIsPending(node.data as CodeBlockNodeData);
  const fields = getDocProseFields(node);
  const isProse = fields.length > 0;
  const stringOf = (key: string) =>
    typeof data[key] === "string" ? (data[key] as string) : "";
  // A field emptied while it is being edited stays on screen until the line is left.
  const [kept, setKept] = useState<string[]>([]);
  const prose = fields.flatMap((f) =>
    stringOf(f.key).trim() || kept.includes(f.key)
      ? [{ ...f, value: stringOf(f.key) }]
      : [],
  );
  const description = stringOf("description").trim()
    ? stringOf("description")
    : null;
  // While editing, the first field is always the step's text, even when empty.
  const [main, ...rest] =
    editable && isProse
      ? [
          { ...fields[0]!, value: stringOf(fields[0]!.key) },
          ...prose.filter((f) => f.key !== fields[0]!.key),
        ]
      : prose;
  const keyFields = getDocKeyFields(node);
  const unfinished = useDocUnfinishedStore((state) => state.ids.has(node.id));
  const missing = unfinished ? [] : getDocMissingSettings(node);
  // A description sits under a prompt-driven step's text; on other steps it is the main line, with the setting it would hide underneath.
  // A loop's line that already names its list says what "For each of …" would, so the summary is left out.
  const loopSource = stripJinjaReferences(
    stringOf("loopVariableReference") || stringOf("loopValue"),
  );
  const lineNamesLoopSource =
    node.type === "loop" &&
    loopSource !== "" &&
    stripJinjaReferences(description ?? "").includes(loopSource);
  const settingSummary =
    !isProse && description && !lineNamesLoopSource
      ? getDocSettingSummary(node)
      : null;
  const focus = actions.focusId === node.id;
  const emptyText = getDocMainPlaceholder(node);
  const forEachLoop = node.type === "loop" && data.loopKind !== "while";
  const mainKey = isProse ? fields[0]!.key : "description";
  const line = (
    key: string,
    value: string,
    placeholder: string,
    className: string,
  ) => (
    <DocEditableLine
      value={value}
      editable={editable}
      placeholder={placeholder}
      className={className}
      label={`${node.data.label} ${key}`}
      autoFocus={focus && key === mainKey}
      references={
        actions.edit
          ? () => actions.edit!.references(node.id, false)
          : undefined
      }
      onTab={key === mainKey ? onTab : undefined}
      onBackspaceAtStart={key === mainKey ? onBackspaceAtStart : undefined}
      onMove={onMove}
      onRunStep={onRunStep}
      onSettings={
        actions.edit ? () => actions.openSettings(node.id) : undefined
      }
      caretAtStart={
        key === mainKey ? () => takeCaretAtStart(node.id) : undefined
      }
      multiline
      onChange={(v) => {
        if (!v.trim() && !kept.includes(key)) setKept([...kept, key]);
        actions.edit?.setText(node.id, key, v);
      }}
      onEnter={
        onEnter
          ? (split) => {
              if (key === mainKey) return onEnter(split, key);
              // A secondary line has no twin to split into: Enter at its end adds the step below, mid-text does nothing.
              if (!split?.after.trim()) onEnter(null, key);
            }
          : undefined
      }
      onSlash={onSlash}
      onDone={() => {
        if (kept.includes(key)) setKept(kept.filter((k) => k !== key));
        if (focus) actions.setFocusId(null);
      }}
    />
  );
  return (
    <>
      <div className="min-h-[1.55em] text-base font-medium leading-[1.55]">
        {isProse ? (
          main ? (
            line(main.key, main.value, emptyText, "")
          ) : (
            <span className="font-normal text-muted-foreground">
              {emptyText}
            </span>
          )
        ) : forEachLoop ? (
          <div className="flex items-baseline gap-1.5">
            {!loopLineLeadsIn(description) && (
              <span className="shrink-0">For each of</span>
            )}
            <span className="min-w-0 flex-1">
              {editable ? (
                line(
                  "description",
                  description ?? "",
                  stringOf("loopVariableReference") ||
                    stringOf("loopValue") ||
                    "@ pick a list",
                  "",
                )
              ) : (
                <DocText
                  value={
                    description ??
                    (stringOf("loopVariableReference") || stringOf("loopValue"))
                  }
                />
              )}
            </span>
          </div>
        ) : editable ? (
          line("description", description ?? "", getDocStepSummary(node), "")
        ) : (
          <DocText value={description ?? getDocStepSummary(node)} />
        )}
      </div>
      {rest.map((f) => (
        <div
          key={f.key}
          className="flex gap-1.5 text-[13.5px] leading-[1.55] text-muted-foreground"
        >
          <span className="shrink-0">{f.label}</span>
          {editable ? (
            <span className="min-w-0 flex-1 text-foreground">
              {line(f.key, f.value, "", "")}
            </span>
          ) : (
            <DocText value={f.value} className="text-foreground" />
          )}
        </div>
      ))}
      {isProse && (description || kept.includes("description")) && (
        <div className="flex gap-1.5 text-[13.5px] leading-[1.55] text-muted-foreground">
          <span className="shrink-0">Description</span>
          {editable ? (
            <span className="min-w-0 flex-1 text-foreground">
              {line("description", description ?? "", "", "")}
            </span>
          ) : (
            <DocText value={description ?? ""} className="text-foreground" />
          )}
        </div>
      )}
      {node.type === "codeBlock" && (goalBuild || goalNotApplied) && (
        <div
          role="status"
          className="text-[12.5px] leading-[1.4] text-muted-foreground"
        >
          {goalBuild === "building"
            ? "Applying the new Goal…"
            : goalBuild === "queued"
              ? "Waiting to apply the new Goal…"
              : "Goal changed, not applied yet. Save to apply it."}
        </div>
      )}
      {missing.length > 0 && (
        <div className="mb-0.5 mt-1 flex flex-wrap gap-1.5">
          {missing.map((label) => (
            <span
              key={label}
              data-doc-missing={label}
              className="inline-flex items-center rounded-md border border-destructive/40 px-2 py-px text-[12.5px] text-destructive"
            >
              {label} required
            </span>
          ))}
        </div>
      )}
      {settingSummary && (
        <div className="text-[13.5px] leading-[1.55] text-muted-foreground">
          <DocText value={settingSummary} />
        </div>
      )}
      {keyFields.length > 0 && (
        <div className="mb-0.5 mt-1 flex flex-wrap gap-1.5">
          {keyFields.map((f) => (
            <span
              key={f.key}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border px-2 py-px text-[12.5px]"
            >
              <span className="shrink-0 text-muted-foreground">{f.label}</span>
              <DocText value={f.value} className="min-w-0" />
            </span>
          ))}
        </div>
      )}
    </>
  );
}
