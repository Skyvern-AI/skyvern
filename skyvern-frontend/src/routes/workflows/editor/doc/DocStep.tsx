import {
  Fragment,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useDocUnfinishedStore } from "./docUnfinished";
import { stripJinjaReferences } from "./docTextUtils";
import {
  codeBlockUses,
  SKIP_KEYS_FOR_JINJA_CHECK,
  transformStringFieldsInObject,
} from "../jinjaReferences";
import { goalChangeIsPending } from "../workflowEditorUtils";
import type { CodeBlockNodeData } from "../nodes/CodeBlockNode/types";
import {
  ChevronRightIcon,
  GearIcon,
  PlusIcon,
  ViewVerticalIcon,
} from "@radix-ui/react-icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/util/utils";
import { DocKeyLabel, DocKeyTooltip } from "./DocKeyLabel";
import type { WorkflowBlockNode } from "../nodes";
import { announceDoc, announceMoved, announceTurnedInto } from "./docAnnounce";
import { workflowBlockTitle } from "../nodes/types";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import {
  customSettingsText,
  docCustomSettings,
  docKeySettings,
  docStepShowsKey,
  type DocSettingRow,
  yamlKey,
} from "./docSettingsChips";
import { docDetailsText } from "./docDetailsText";
import {
  docBlockType,
  docStepCaption,
  docTagClasses,
  docTagColor,
  docStepProducesData,
  getDocMainPlaceholder,
  getDocMissingSettings,
  loopLineLeadsIn,
  getDocFilledProseFields,
  getDocProseFields,
  getDocSettingSummary,
  getDocStepSummary,
  loopHasNoList,
  loopList,
} from "./docStepRegistry";
import { DocRunStepButton } from "./DocRunStepButton";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import { DocText, docReferencePill } from "./DocText";
import type { DocPlace } from "./docEditApi";
import {
  DocDeleteConfirm,
  DocEditableLine,
  DocMoveToDialog,
  DocOutputName,
  DocStepPicker,
} from "./DocEditing";
import { DocImages } from "./DocImages";
import { docImageDropProps, useDocImageFiles } from "./useDocImageFiles";
import { docImagesOf } from "./docGraphEdits";
import {
  useDocStepAdded,
  useDocStepChangedKeys,
  useDocStepPending,
} from "./DocProposalView";
import { DocOneActionHint } from "./DocOneActionHint";
import { isDocUrlOfferVisible } from "./docOneAction";
import { DocStepMeta } from "./DocStepMeta";
import { DocStepName } from "./DocStepName";
import { useDocGoalBuild } from "./useDocGoalBuild";
import { useDocStepMetaRows } from "./useDocStepMetaRows";
import {
  docShortcut,
  takeCaretAtStart,
  type LineSplit,
  type PickerOpener,
} from "./docKeys";

/** Where a step sits, for the moves its tag menu offers. Absent when the step's place can't be edited. */
export type DocStepPlace = DocPlace & { count: number; isFinally: boolean };

const tagClass = (
  blockType: ReturnType<typeof docBlockType>,
  pending = false,
) =>
  cn(
    "inline-flex h-[21px] shrink-0 items-center whitespace-nowrap rounded px-[6px] text-[12.5px] font-semibold leading-none",
    docTagClasses[docTagColor(blockType)],
    pending &&
      "border border-dashed border-current bg-transparent px-[5px] opacity-80 dark:bg-transparent",
  );

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
  const playing = useBlockPlayRegistry((s) => s.handles[node.id]?.playing);
  // A step that is running can't be moved, changed into another type or deleted.
  const running = Boolean(playing);
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
          <DropdownMenuItem
            onSelect={() => actions.openSettings(node.id)}
            aria-keyshortcuts={docShortcut(".").aria}
          >
            Settings
            <DropdownMenuShortcut>
              {docShortcut(".").label}
            </DropdownMenuShortcut>
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
            data-testid="doc-step-tag"
            aria-haspopup="menu"
            aria-expanded={menuAt === "tag"}
            onClick={() => toggleFrom("tag")}
            onKeyDown={openOnArrow("tag")}
            title="Click for actions"
            aria-label={`${blockType ? workflowBlockTitle[blockType] : node.type} step ${node.data.label}, actions`}
            className={cn(
              tagClass(blockType, pending),
              "outline-none hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            {blockType ? workflowBlockTitle[blockType] : node.type}
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
  if (actions.edit && place)
    return (
      <DocStepMenu node={node} place={place} besideChevron={besideChevron} />
    );
  return (
    <span data-testid="doc-step-tag" className={tagClass(blockType, pending)}>
      {blockType ? workflowBlockTitle[blockType] : node.type}
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
  const changed = docCustomSettings(node)?.length ?? 0;
  // Without a changed count the gear is not rendered at rest (display: none), so nothing is reserved for it.
  return (
    <DocKeyTooltip
      content={
        <>
          <DocKeyLabel
            label={changed ? customSettingsText(changed) : "Settings"}
            keys="."
          />
        </>
      }
    >
      <button
        type="button"
        aria-label={`Settings for ${node.data.label}${changed ? `, ${customSettingsText(changed)}` : ""}`}
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
  settings = true,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  showRunControl: boolean;
  // A step's settings open from its "Details" instead.
  settings?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {showRunControl && <DocRunStepButton node={node} disabled={!editable} />}
      {settings && <DocSettingsButton node={node} editable={editable} />}
    </span>
  );
}

/** A step's place in its list, "2" or "3.1". */
export function DocStepNumber({ number }: { number?: string }) {
  return number ? (
    <span
      data-testid="doc-step-number"
      className="text-[12.5px] tabular-nums text-muted-foreground"
    >
      {number}
    </span>
  ) : null;
}

export function DocStepHeader({
  node,
  editable,
  showRunControl = editable,
  extra,
  disclosure,
  place,
  number,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  showRunControl?: boolean;
  extra?: ReactNode;
  disclosure?: { open: boolean; onToggle: () => void };
  place?: DocStepPlace;
  number?: string;
}) {
  return (
    <div>
      {/* The grip and the loop arrow are placed against this row, so they stay beside the number. */}
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
        <DocStepNumber number={number} />
        <DocTag
          node={node}
          place={editable ? place : undefined}
          besideChevron={Boolean(disclosure)}
        />
        {extra}
        <DocStepControls
          node={node}
          editable={editable}
          showRunControl={showRunControl}
          settings={false}
        />
      </div>
      <DocStepName node={node} editable={editable} />
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
  onAddNote,
  hasFix = false,
  renamable = false,
}: {
  node: WorkflowBlockNode;
  editable?: boolean;
  // Opens the note line below the step, as Enter at the end of a step that needs no text does.
  onAddNote?: () => void;
  // The step's problem line (what it still needs before it can run) is on screen under it.
  hasFix?: boolean;
  // Whether the output name can be renamed here.
  renamable?: boolean;
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
  const goalBuild = useDocGoalBuild(node.data.label);
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
  const prose = getDocFilledProseFields(node, kept);
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
  const keyFields = useMemo(
    () => docKeySettings(node, actions.loginCredentialKeys),
    [node, actions.loginCredentialKeys],
  );
  const settingRows = useDocStepMetaRows(node, keyFields);
  const metaRows = settingRows.line;
  const unfinished = useDocUnfinishedStore((state) => state.ids.has(node.id));
  // A dashed chip already asks for the setting, so it is not also listed as required.
  const missing = unfinished
    ? []
    : getDocMissingSettings(node, (key) =>
        metaRows.some((row) => row.unset && row.key === yamlKey(key)),
      );
  // A description sits under a prompt-driven step's text; on other steps it is the main line, with the setting it would hide underneath.
  // A loop's line that already names its list says what "For each of …" would, so the summary is left out.
  const loopSource = stripJinjaReferences(loopList(data));
  const lineNamesLoopSource =
    node.type === "loop" &&
    loopSource !== "" &&
    stripJinjaReferences(description ?? "").includes(loopSource);
  // Under a description, a for-each loop with no list would only add "For each item"; where its problem line shows, that says what is missing.
  const summaryYieldsToProblem =
    hasFix && node.type === "loop" && loopHasNoList(data);
  const settingSummary =
    !isProse && description && !lineNamesLoopSource && !summaryYieldsToProblem
      ? getDocSettingSummary(node)
      : null;
  const focus = actions.focusId === node.id;
  const emptyText = getDocMainPlaceholder(node, editable);
  const forEachLoop = node.type === "loop" && data.loopKind !== "while";
  const mainKey = isProse ? fields[0]!.key : "description";
  // Read-only, an empty first field gives the main line to the next filled one; that line is the one that folds.
  const shownMainKey = isProse ? (main?.key ?? mainKey) : mainKey;
  // A proposal that adds the step or changes its shown text keeps that text whole, so nothing is accepted unseen.
  const proposalChanged = useDocStepChangedKeys(node);
  const proposalAdded = useDocStepAdded(node);
  const revealText =
    proposalAdded || proposalChanged.includes(yamlKey(shownMainKey));
  // The summary line shows a URL's host only and cuts long values, so a value the proposal sets is listed whole in
  // Details, which opens for it; so is every value of a step whose settings cannot be opened.
  const lineTouched = settingRows.proposed;
  const detailsFields = keyFields.filter(
    (f) =>
      settingRows.details.includes(f) ||
      (metaRows.includes(f) && (lineTouched(f.key) || !settingRows.editable)),
  );
  // Keep a pre-existing offer reachable on focus, but hide it after typing until the line is left.
  const lineMode = `${editable}-${isProse}`;
  const [focusedMode, setFocusedMode] = useState<string | null>(null);
  const menuActive = useRef(false);
  const lineContainer = useRef<HTMLDivElement>(null);
  const [focusStart, setFocusStart] = useState<{
    text: string;
    requiredOffer: boolean;
  } | null>(null);
  // A lock can unmount the focused line without a blur, so discard its snapshot.
  if (!editable && focusedMode !== null) setFocusedMode(null);
  const candidate =
    editable && actions.edit ? actions.promptUrlOffer(node) : null;
  const focused = focusedMode === lineMode;
  const urlOffer =
    !focused || focusStart?.text === stringOf(shownMainKey) ? candidate : null;
  const [, refreshHint] = useState(0);
  const urlOfferVisible = isDocUrlOfferVisible(node, urlOffer);
  const requiredUrlOfferVisible =
    urlOfferVisible && Boolean(urlOffer?.required);
  const hideUrlChip = focused
    ? Boolean(focusStart?.requiredOffer)
    : requiredUrlOfferVisible;
  const hint = editableProp && (
    <DocOneActionHint
      node={node}
      urlOffer={urlOffer}
      onDismiss={() => refreshHint((n) => n + 1)}
    />
  );
  const textCollapse = {
    open: actions.textOpen(node.id),
    setOpen: (open: boolean) => actions.setTextOpen(node.id, open),
    name: docStepCaption(node) ?? node.data.label,
  };
  // A screenshot pasted into the step's text goes under the text, never inside it.
  const imageFiles = useDocImageFiles({
    ownerNodeId: node.id,
    placement: "block",
  });
  const addFiles = editable && actions.edit ? imageFiles.addFiles : null;
  const line = (
    key: string,
    value: string,
    placeholder: string,
    className: string,
  ) => (
    <DocEditableLine
      onMenuActiveChange={(open) => {
        menuActive.current = open;
        if (!open && !lineContainer.current?.contains(document.activeElement)) {
          setFocusedMode(null);
          setFocusStart(null);
          setHeldRestInView(null);
        }
      }}
      onFiles={addFiles ?? undefined}
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
      collapse={key === shownMainKey && !revealText ? textCollapse : undefined}
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
  const restLines = rest.map((f) => (
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
  ));
  // With the step's text empty, a filled secondary line (Terminate if) is all the step says, so it stays in view.
  // Held while the main line is focused, so clearing it and typing again does not move the lines mid-edit.
  const liveRestInView = isProse && !main?.value.trim();
  const [heldRestInView, setHeldRestInView] = useState<boolean | null>(null);
  // Taking editing away (a save lock, a run) ends the edit, so its held placement goes too.
  if (!editable && heldRestInView !== null) setHeldRestInView(null);
  const restInView =
    focusedMode === lineMode && heldRestInView !== null
      ? heldRestInView
      : liveRestInView;
  const descriptionInDetails =
    isProse && (description !== null || kept.includes("description"));
  // The saved keys of the lines shown with Details closed.
  const lineKeys = (
    isProse
      ? [
          main?.key ?? fields[0]!.key,
          ...(restInView ? rest.map((f) => f.key) : []),
        ]
      : ["description"]
  )
    .map(yamlKey)
    .concat(metaRows.filter((f) => !lineTouched(f.key)).map((f) => f.key));
  const keySettings = (
    <>
      {detailsFields.length > 0 && (
        <dl className="mb-1 grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]">
          {detailsFields.map((f) => (
            <Fragment key={f.key}>
              <dt className="text-muted-foreground">{f.label}</dt>
              <dd
                className={cn(
                  "min-w-0 [overflow-wrap:anywhere]",
                  f.unset && "text-muted-foreground",
                )}
              >
                <DocText value={f.value} />
                {f.unset && f.addLabel && editableProp && actions.edit && (
                  <button
                    type="button"
                    data-doc-add-row={f.key}
                    onClick={() => actions.openSettings(node.id)}
                    className="ml-2 inline-flex items-center gap-0.5 rounded px-1 text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <PlusIcon aria-hidden className="size-3" />
                    Add {f.addLabel}
                  </button>
                )}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
      {!restInView && restLines}
      {descriptionInDetails && (
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
    </>
  );
  return (
    <>
      <DocStepMeta
        node={node}
        rows={metaRows.filter(
          (row) => !(hideUrlChip && row.key === "url" && row.unset),
        )}
      />
      <div
        ref={lineContainer}
        className="min-h-[1.55em] text-base font-normal leading-[1.55]"
        {...docImageDropProps(addFiles)}
        onFocus={(event) => {
          if (isCollapseControl(event.target) || menuActive.current) return;
          setFocusStart({
            text: stringOf(shownMainKey),
            requiredOffer: Boolean(
              candidate?.required && isDocUrlOfferVisible(node, candidate),
            ),
          });
          setFocusedMode(lineMode);
          setHeldRestInView((held) => held ?? liveRestInView);
        }}
        onBlur={(event) => {
          if (isCollapseControl(event.target) || menuActive.current) return;
          if (
            focused &&
            focusStart?.text !== stringOf(shownMainKey) &&
            isDocUrlOfferVisible(node, candidate)
          )
            announceDoc("A URL suggestion is available.");
          setFocusedMode(null);
          setFocusStart(null);
          setHeldRestInView(null);
        }}
      >
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
                  loopList(data) || "@ pick a list",
                  "",
                )
              ) : (
                <DocText value={description ?? loopList(data)} />
              )}
            </span>
          </div>
        ) : editable ? (
          line("description", description ?? "", getDocStepSummary(node), "")
        ) : (
          <DocText value={description ?? getDocStepSummary(node)} />
        )}
      </div>
      <DocImages
        slot={{ ownerNodeId: node.id, placement: "block" }}
        images={docImagesOf(node).filter(
          (image) => image.placement === "block",
        )}
        editable={editable && Boolean(actions.edit)}
        onRetry={imageFiles.retry}
      />
      {restInView && restLines}
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
      {requiredUrlOfferVisible && hint}
      <DocStepSummary
        node={node}
        lineKeys={lineKeys}
        hasDetailsLines={
          (!restInView && rest.length > 0) ||
          descriptionInDetails ||
          detailsFields.some((f) => f.changed)
        }
        editable={editableProp}
        renamable={renamable}
        keySettings={keySettings}
        onAddNote={editableProp ? onAddNote : undefined}
      />
      {!requiredUrlOfferVisible && hint}
    </>
  );
}

// Show more is not the step's text: focusing it neither hides the URL offer nor holds the lines in place.
const isCollapseControl = (target: EventTarget) =>
  target instanceof HTMLElement && target.dataset.docCollapse !== undefined;

// The Doc does not show a Code step's Python, so its reads are not listed.
const CODE_SKIP_KEYS = new Set([...SKIP_KEYS_FOR_JINJA_CHECK, "code", "steps"]);

// Every input and result the step reads: declared keys, then `{{ }}` reads, including those in its main line.
function stepReferences(node: WorkflowBlockNode): string[] {
  const texts: string[] = [];
  transformStringFieldsInObject(
    node.data,
    (text) => {
      texts.push(text);
      return text;
    },
    node.type === "codeBlock" ? CODE_SKIP_KEYS : SKIP_KEYS_FOR_JINJA_CHECK,
  );
  const declared = (node.data as Record<string, unknown>).parameterKeys;
  return [
    ...new Set([
      ...(Array.isArray(declared) ? (declared as string[]) : []),
      ...texts.flatMap((text) => codeBlockUses([], text)),
    ]),
  ];
}

// Text Prompt, Web Search and the file and PDF parsers keep their output schema in jsonSchema.
const outputFieldNames = (data: Record<string, unknown>) =>
  schemaFieldNames(data.dataSchema ?? data.jsonSchema);

function schemaFieldNames(raw: unknown): string[] {
  let schema: unknown;
  try {
    schema = typeof raw === "string" ? JSON.parse(raw) : raw;
  } catch {
    return [];
  }
  const shape = schema as {
    properties?: unknown;
    items?: { properties?: unknown };
  } | null;
  const properties = shape?.properties ?? shape?.items?.properties;
  return properties && typeof properties === "object"
    ? Object.keys(properties)
    : [];
}

/** The Details disclosure: key settings, inputs and outputs, and the settings changed from their defaults. */
function DocStepSummary({
  node,
  lineKeys,
  hasDetailsLines,
  editable,
  renamable,
  keySettings,
  onAddNote,
}: {
  node: WorkflowBlockNode;
  onAddNote?: () => void;
  lineKeys: string[];
  // Details holds a filled line or key setting of the step.
  hasDetailsLines: boolean;
  editable: boolean;
  renamable: boolean;
  keySettings: ReactNode;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const custom = docCustomSettings(node);
  // A proposal that changes anything the step does not show with Details closed opens it, so the change is not
  // hidden; a key Details may not list (a setting reset to its default, a new key) opens it too.
  const changed = useDocStepChangedKeys(node);
  // A step the proposal adds is all new: Details opens when it holds anything, so nothing is accepted unseen.
  const added = useDocStepAdded(node);
  const open =
    actions.detailsOpen(node.id) ??
    (added
      ? hasDetailsLines ||
        Boolean(custom?.length) ||
        stepReferences(node).length > 0 ||
        docStepProducesData(node) ||
        outputFieldNames(node.data as Record<string, unknown>).length > 0
      : changed.some((key) => !docStepShowsKey(node, key, lineKeys)));
  const setOpen = (next: boolean) => actions.setDetailsOpen(node.id, next);
  const panelId = useId();
  const noteButton = useRef<HTMLButtonElement>(null);
  // Escape on the empty note this button opened leaves focus on the page, so it comes back here.
  const [noteOpened, setNoteOpened] = useState(false);
  const pendingNote = actions.pendingNote;
  const escaped = useRef(false);
  useEffect(() => {
    if (!noteOpened) return;
    if (pendingNote) {
      escaped.current = false;
      const onKey = (event: globalThis.KeyboardEvent) => {
        if (event.key === "Escape") escaped.current = true;
      };
      document.addEventListener("keydown", onKey, true);
      return () => document.removeEventListener("keydown", onKey, true);
    }
    setNoteOpened(false);
    if (escaped.current)
      requestAnimationFrame(() => noteButton.current?.focus());
  }, [noteOpened, pendingNote]);
  return (
    <>
      <div className="mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
        <DocKeyTooltip
          content={
            custom?.length
              ? customSettingsText(custom.length)
              : docDetailsText.tooltip
          }
        >
          <button
            type="button"
            aria-label={docDetailsText.toggleFor(
              docStepCaption(node) ?? node.data.label,
            )}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen(!open)}
            className="-ml-1 inline-flex items-center gap-0.5 rounded px-1 text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronRightIcon
              aria-hidden
              className={cn("size-3 transition-transform", open && "rotate-90")}
            />
            {docDetailsText.toggle}
          </button>
        </DocKeyTooltip>
        {onAddNote && (
          <button
            type="button"
            ref={noteButton}
            aria-label={`Add note below ${docStepCaption(node) ?? node.data.label}`}
            onClick={() => {
              onAddNote?.();
              setNoteOpened(true);
            }}
            className="inline-flex items-center gap-0.5 rounded px-1 text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <PlusIcon aria-hidden className="size-3" />
            Note
          </button>
        )}
      </div>
      {open && (
        <DocStepDetails
          id={panelId}
          node={node}
          editable={editable}
          renamable={renamable}
          keySettings={keySettings}
          custom={custom}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function DocStepDetails({
  id,
  node,
  editable,
  renamable,
  keySettings,
  custom,
  onClose,
}: {
  id: string;
  node: WorkflowBlockNode;
  editable: boolean;
  renamable: boolean;
  keySettings: ReactNode;
  custom: DocSettingRow[] | null;
  onClose: () => void;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const inputs = stepReferences(node);
  const saves = docStepProducesData(node);
  const fields = outputFieldNames(node.data as Record<string, unknown>);
  const heading =
    "mt-2 text-[12px] font-medium text-muted-foreground first:mt-0";
  return (
    <div
      id={id}
      className="mt-1 rounded-md border border-border px-3 py-2 text-[13px]"
    >
      {keySettings}
      {inputs.length > 0 && (
        <>
          <p className={heading}>{docDetailsText.inputs}</p>
          <div className="flex flex-wrap items-baseline gap-1">
            {inputs.map((ref) => (
              <span key={ref} className={docReferencePill}>
                {ref}
              </span>
            ))}
          </div>
        </>
      )}
      {(saves || fields.length > 0) && (
        <>
          <p className={heading}>{docDetailsText.outputs}</p>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            {saves &&
              (renamable ? (
                <DocOutputName id={node.id} label={node.data.label} />
              ) : (
                <span
                  title={`${node.data.label}_output`}
                  className="inline-block min-w-0 max-w-full rounded bg-muted px-[5px] align-baseline font-medium leading-[1.25] [overflow-wrap:anywhere]"
                >
                  {node.data.label}_output
                </span>
              ))}
            {fields.length > 0 && (
              <span className="text-muted-foreground">
                {docDetailsText.outputFields}: {fields.join(", ")}
              </span>
            )}
          </div>
        </>
      )}
      {custom && custom.length > 0 && (
        <p className={heading}>{docDetailsText.custom}</p>
      )}
      {custom && custom.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5">
          {custom.map((row) => (
            <Fragment key={row.key}>
              <dt className="text-muted-foreground">{row.label}</dt>
              <dd className="min-w-0 whitespace-pre-line [overflow-wrap:anywhere]">
                {row.value}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
      {/* A read-only Doc's side panel cannot show a step's settings, so the link is left out. */}
      {editable && (
        <button
          type="button"
          onClick={() => {
            onClose();
            actions.openSettings(node.id);
          }}
          className="ml-auto mt-1.5 flex w-fit items-center gap-1.5 rounded px-1 text-[12.5px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {docDetailsText.morePanel}
          <ViewVerticalIcon aria-hidden className="size-3.5" />
        </button>
      )}
    </div>
  );
}
