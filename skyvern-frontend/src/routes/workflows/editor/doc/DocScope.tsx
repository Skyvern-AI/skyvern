import {
  Fragment,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ContextType,
  type RefObject,
  type DragEvent,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { cn } from "@/util/utils";
import {
  ExclamationTriangleIcon,
  LockClosedIcon,
  PlusIcon,
} from "@radix-ui/react-icons";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { workflowBlockTitle } from "../nodes/types";
import { loopSchemaStepId } from "./docProblems";
import {
  countDocSteps,
  isNumberedItem,
  type DocItem,
  type DocNoteItem,
} from "./buildDocTree";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import {
  docBlockType,
  docStepCaption,
  getDocMissingSettings,
  getDocProseFields,
} from "./docStepRegistry";
import { DocConditional } from "./DocConditional";
import { DocStepBody, DocStepHeader, type DocStepPlace } from "./DocStep";
import {
  DocProposalBranchRemovals,
  DocProposalLoopRemovals,
  DocProposalNoteMark,
  DocProposalStepMark,
} from "./DocProposalView";
import { DocEditableLine, DocInsertPopover } from "./DocEditing";
import { NOTE_MAX_LENGTH, type DocScopeRef } from "./docGraphEdits";
import { TOP_SCOPE, type DocPlace } from "./docEditApi";
import { DocStepResult } from "./DocStepResult";
import { DocDemonstrationLine } from "./DocDemonstrationLine";
import { demonstrationLineIndex } from "./docDemonstration";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import {
  armNextCaretAtStart,
  requestCaretAtStart,
  requestPickerOnLine,
  runStepByKey,
  takePickerOnLine,
  useDocTouch,
  type LineSplit,
  type PickerOpener,
} from "./docKeys";
import {
  announceAdded,
  announceMoved,
  announceNest,
  announceNestReady,
} from "./docAnnounce";

type PlacedItem = Exclude<DocItem, DocNoteItem>;

const NOTE_PLACEHOLDER = "Write a note, or type / for a step";
// Phones have no Tab key, so the placeholder doesn't offer it there.
const noTabKey = () =>
  window.matchMedia?.("(hover: none), (max-width: 719px)").matches ?? false;

// The text a note keeps on its owner step.
function noteTextOfNode(
  byId: Map<string, AppNode>,
  note: { ownerNodeId: string; placement: "above" | "trailing" },
) {
  const data = byId.get(note.ownerNodeId)?.data as
    | Record<string, unknown>
    | undefined;
  const text = data?.[note.placement === "above" ? "notes" : "trailingNotes"];
  return typeof text === "string" ? text : "";
}

// Whether `text` fits the line on one row in the line's own font. Null text always fits.
function useFitsOneRow(ref: RefObject<HTMLElement>, text: string | null) {
  const [fits, setFits] = useState(true);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !text) return;
    const context = document.createElement("canvas").getContext("2d");
    if (!context) return;
    const measure = () => {
      context.font = getComputedStyle(el).font;
      setFits(context.measureText(text).width <= el.clientWidth);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, text]);
  return fits;
}

const sameScope = (a: DocScopeRef | undefined, b: DocScopeRef | undefined) =>
  Boolean(
    a &&
    b &&
    a.parentId === b.parentId &&
    a.conditionalBranchId === b.conditionalBranchId,
  );
export type DocNoteProblem = {
  code: string;
  message: string;
};

function DocReadOnly({
  reason,
  children,
}: {
  reason: string;
  children?: ReactNode;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  return (
    <div className="my-0.5 rounded-lg border border-dashed border-border px-3 py-2 text-[13.5px] text-muted-foreground">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex min-w-[min(220px,100%)] flex-1 items-start gap-2">
          <LockClosedIcon className="mt-[3px] size-3.5 shrink-0" />
          <span>
            <b className="font-[550] text-foreground">
              Edit this part in Visual.
            </b>{" "}
            {reason}
          </span>
        </span>
        <button
          type="button"
          onClick={actions.openVisual}
          className="rounded text-[12.5px] text-foreground underline underline-offset-4 outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Open Visual
        </button>
      </div>
      {children}
    </div>
  );
}

function stepName(node: AppNode | undefined) {
  const type = node && isWorkflowBlockNode(node) ? docBlockType(node) : null;
  return type ? workflowBlockTitle[type] : "next";
}

function noteReaders(
  note: DocNoteItem,
  byId: Map<string, AppNode>,
  previousNoteReader: (nodeId: string) => string | undefined,
): string {
  const owner = stepName(byId.get(note.ownerNodeId));
  if (note.placement === "trailing")
    return `The agent reads this in the ${owner} step above.`;
  // The run's own sequence decides the other reader, which is not always the step drawn above.
  const previousId = previousNoteReader(note.ownerNodeId);
  const above = previousId ? stepName(byId.get(previousId)) : null;
  return above
    ? `The agent reads this in the ${above} step before it and the ${owner} step below.`
    : `The agent reads this in the ${owner} step below.`;
}

// A run of steps that share a reason gets one banner.
function previousReason(items: DocItem[], index: number) {
  for (let i = index - 1; i >= 0; i--) {
    const item = items[i]!;
    if (item.kind === "unplaced") return undefined;
    if (item.kind !== "note") return item.readOnlyReason;
  }
  return undefined;
}

function DocStepProblem({
  nodeId,
  problems,
  byId,
  editable,
}: {
  nodeId: string;
  problems: ReadonlyMap<string, DocNoteProblem>;
  byId: Map<string, AppNode>;
  editable: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const problem = problems.get(`${nodeId}:step`);
  // What the step still needs before it can run, found as the draft is typed.
  const fix = problems.get(`${nodeId}:fix`);
  const node = byId.get(nodeId);
  const schemaStep =
    fix && node && editable && actions.edit
      ? byId.get(loopSchemaStepId(node, [...byId.values()]) ?? "")
      : undefined;
  return (
    <>
      {fix && (
        <ul data-doc-step-fix className="mt-0.5 flex flex-col">
          {fix.message.split("\n").map((message) => (
            <li
              key={message}
              className="flex gap-1.5 text-[13px] leading-[1.55] text-amber-700 [overflow-wrap:anywhere] dark:text-amber-300"
            >
              <ExclamationTriangleIcon
                aria-hidden
                className="mt-[3px] size-3.5 shrink-0"
              />
              <span className="min-w-0">{message}</span>
            </li>
          ))}
        </ul>
      )}
      {schemaStep && (
        <button
          type="button"
          onClick={() => actions.openSettings(schemaStep.id)}
          className="mt-1 rounded-md border border-border px-2 py-0.5 text-[12.5px] text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
        >
          {`Open ${docStepCaption(schemaStep) ?? schemaStep.data.label}'s Data Schema`}
        </button>
      )}
      {problem && (
        <p
          data-doc-step-problem
          className="mt-1 whitespace-pre-line text-sm font-medium text-amber-700 dark:text-amber-300"
        >
          {problem.message}
        </p>
      )}
    </>
  );
}

function DocNode({
  item,
  byId,
  readOnly,
  place,
  number,
  onEnter,
  noteProblems,
}: {
  item: Exclude<PlacedItem, { kind: "unplaced" }>;
  byId: Map<string, AppNode>;
  readOnly: boolean;
  place?: DocStepPlace;
  number: string;
  onEnter?: (split: LineSplit | null, key?: string) => void;
  noteProblems: ReadonlyMap<string, DocNoteProblem>;
}) {
  const [inserting, setInserting] = useState(false);
  const finishSlash = useRef<Parameters<PickerOpener>[0] | null>(null);
  const actions = useContext(WorkflowDocActionsContext);
  const open = actions.loopOpen(item.nodeId);
  const node = byId.get(item.nodeId);
  if (!node || !isWorkflowBlockNode(node)) return null;
  const editable =
    actions.editable && !readOnly && !item.readOnlyReason && node.data.editable;
  const showRunControl =
    actions.runControlsVisible &&
    !readOnly &&
    !item.readOnlyReason &&
    node.data.editable;
  const reached = item.reachedFrom ? (
    <span className="text-[12.5px] text-muted-foreground">
      reached from {item.reachedFrom} branches
    </span>
  ) : null;
  if (item.kind === "conditional")
    return (
      <>
        <DocConditional
          node={node}
          item={item}
          byId={byId}
          editable={editable}
          showRunControl={showRunControl}
          place={place}
          number={number}
          onEnter={onEnter}
          renderChildren={(children, scope, branchId) => (
            <>
              <DocScope
                items={children}
                byId={byId}
                scope={scope}
                readOnly={readOnly || Boolean(item.readOnlyReason)}
                emptyText={scope ? "No steps in this branch yet" : undefined}
                noteProblems={noteProblems}
              />
              <DocProposalBranchRemovals branchId={branchId} />
            </>
          )}
        />
        <DocStepProblem
          nodeId={item.nodeId}
          problems={noteProblems}
          byId={byId}
          editable={editable}
        />
      </>
    );
  const inside = item.kind === "loop" ? countDocSteps(item.children) : 0;
  return (
    <>
      <div className="group/line rounded py-[3px]">
        <DocStepHeader
          node={node}
          editable={editable}
          showRunControl={showRunControl}
          place={place}
          number={number}
          disclosure={
            item.kind === "loop"
              ? { open, onToggle: () => actions.toggleLoop(item.nodeId) }
              : undefined
          }
          extra={
            <>
              {reached}
              {item.kind === "loop" && !open && (
                <span className="text-[12.5px] text-muted-foreground">
                  {inside} step{inside === 1 ? "" : "s"} inside
                </span>
              )}
            </>
          }
        />
        <DocInsertPopover
          open={inserting}
          onOpenChange={setInserting}
          onDismiss={() => finishSlash.current?.(true)}
          onDemonstrate={
            place &&
            item.kind === "step" &&
            !place.isFinally &&
            actions.edit?.canReplaceBlankStep(node.id)
              ? demonstrateFrom(actions, place, finishSlash, node.id)
              : demonstrateFrom(
                  actions,
                  place && { scope: place.scope, index: place.index + 1 },
                  finishSlash,
                )
          }
          onPick={(type) => {
            finishSlash.current?.(false);
            if (!place || !actions.edit) return;
            // "/" on a line changes that line's step type; a loop or If can't be turned into, so it goes in below.
            // A step with settings of its own keeps them, so it is not turned into another type here.
            if (
              item.kind === "step" &&
              type !== "loop" &&
              type !== "conditional" &&
              actions.edit.canBePlainText(node.id)
            ) {
              if (type !== node.type) actions.edit.turnInto(node.id, type);
              // The step may already be the focus target; clear it so setting it again reopens the line.
              actions.setFocusId(null);
              requestAnimationFrame(() => actions.setFocusId(node.id));
              return;
            }
            // "/" on an empty step replaces it, whatever the type picked.
            const added =
              item.kind === "step" &&
              !place.isFinally &&
              actions.edit.isEmptyStep(node.id)
                ? actions.edit.replaceStep(node.id, type)
                : actions.edit.insertStep(
                    { scope: place.scope, index: place.index + 1 },
                    type,
                  );
            actions.setFocusId(added);
            if (added) announceAdded(type);
          }}
        >
          <div>
            <DocStepBody
              node={node}
              editable={editable && Boolean(actions.edit)}
              hasFix={noteProblems.has(`${node.id}:fix`)}
              renamable={editable && Boolean(place)}
              onEnter={onEnter}
              onMove={
                place && actions.edit && !place.isFinally
                  ? (delta) => {
                      const to = place.index + delta;
                      if (to < 0 || to >= place.count) return true;
                      const moved = actions.edit!.move(node.id, {
                        scope: place.scope,
                        index: to,
                      });
                      // The moved line remounts, so refocus after it.
                      if (moved) {
                        requestAnimationFrame(() =>
                          actions.setFocusId(node.id),
                        );
                        announceMoved(delta < 0 ? "up" : "down");
                      }
                      return true;
                    }
                  : undefined
              }
              onRunStep={() => runStepByKey(node.id)}
              onSlash={
                place
                  ? (keep) => {
                      finishSlash.current = keep;
                      setInserting(true);
                    }
                  : undefined
              }
              onTab={
                place && actions.edit
                  ? (outdent) => {
                      const kind = actions.edit!.nest(node.id, outdent);
                      if (!kind) return false;
                      announceNest(kind, outdent);
                      // The moved line remounts and the old one's blur clears the focus, so refocus after it.
                      requestAnimationFrame(() => actions.setFocusId(node.id));
                      return true;
                    }
                  : undefined
              }
              onBackspaceAtStart={
                place &&
                actions.edit &&
                item.kind === "step" &&
                actions.edit.canBePlainText(node.id)
                  ? () => {
                      actions.setPendingNote(
                        actions.edit!.toPlainText(node.id),
                      );
                      return true;
                    }
                  : undefined
              }
            />
          </div>
        </DocInsertPopover>
        <DocStepProblem
          nodeId={item.nodeId}
          problems={noteProblems}
          byId={byId}
          editable={editable}
        />
        <DocStepResult label={node.data.label} />
      </div>
      {item.kind === "loop" && open && (
        <div className="mb-1 ml-[11px] mt-0.5 border-l border-border pl-4">
          <DocScope
            items={item.children}
            byId={byId}
            scope={{ parentId: item.nodeId, conditionalBranchId: null }}
            readOnly={readOnly || Boolean(item.readOnlyReason)}
            emptyText="No steps in this loop yet"
            noteProblems={noteProblems}
          />
          <DocProposalLoopRemovals loopLabel={node.data.label} />
        </div>
      )}
    </>
  );
}

// The menu's "Demonstrate task" for a place; a "/" line closes without typing the slash first.
function demonstrateFrom(
  actions: ContextType<typeof WorkflowDocActionsContext>,
  place: DocPlace | undefined,
  finishSlash?: MutableRefObject<((keepSlash: boolean) => void) | null>,
  replacing?: string,
) {
  const start = place ? actions.demonstrateAt(place, replacing) : null;
  return start
    ? () => {
        finishSlash?.current?.(false);
        start();
      }
    : null;
}

// Drop target handlers: the target highlights while a step is over it.
function useDropTarget(onDrop: (() => void) | undefined) {
  const [over, setOver] = useState(false);
  if (!onDrop) return { over: false, handlers: {} };
  return {
    over,
    handlers: {
      onDragOver: (event: DragEvent) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        if (!over) setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: (event: DragEvent) => {
        event.preventDefault();
        setOver(false);
        onDrop();
      },
    },
  };
}

// A line between steps that takes a dropped step. Its negative margins cancel the extra gap, so nothing shifts.
function DocDropZone({ onDrop }: { onDrop: () => void }) {
  const { over, handlers } = useDropTarget(onDrop);
  return (
    <div aria-hidden className="relative -my-3 h-3" {...handlers}>
      <div
        className={cn(
          "pointer-events-none absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 rounded-full",
          over && "bg-primary",
        )}
      />
    </div>
  );
}

// The line with a round "+" in the gap above a step. It opens an empty line there with the step picker on it; when a
// note already sits above that step, the picker opens on the line itself.
function DocInsertLine({
  place,
  ownerNodeId,
  noteFree,
  label,
}: {
  place: DocPlace;
  ownerNodeId: string;
  noteFree: boolean;
  label: string;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const [open, setOpen] = useState(false);
  const plusRef = useRef<HTMLButtonElement>(null);
  // Hidden until hovered, so on touch it would be an invisible tap target; phones add with Enter, "/" and "+ Add step".
  const touch = useDocTouch();
  if (touch) return null;
  return (
    <DocInsertPopover
      open={open}
      onOpenChange={setOpen}
      onEscape={() => requestAnimationFrame(() => plusRef.current?.focus())}
      onPick={(type) => {
        const added = actions.edit?.insertStep(place, type) ?? null;
        actions.setFocusId(added);
        if (added) announceAdded(type);
      }}
    >
      {/* Its negative margins cancel the gap it sits in, so nothing shifts. */}
      <div
        data-doc-insert-line={ownerNodeId}
        className="group/ins relative -my-3 h-3"
      >
        <div
          aria-hidden
          className={cn(
            "pointer-events-none absolute inset-x-0 top-1/2 border-t-[1.5px] border-border opacity-0 transition-opacity group-focus-within/ins:opacity-100 group-hover/ins:opacity-100",
            open && "opacity-100",
          )}
        />
        <button
          ref={plusRef}
          type="button"
          aria-label={label}
          onClick={() => {
            if (!noteFree) return setOpen(true);
            requestPickerOnLine(`${ownerNodeId}:above`);
            actions.setPendingNote({ ownerNodeId, placement: "above" });
          }}
          className={cn(
            "absolute -left-[30px] top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded-full border-[1.5px] border-muted-foreground/40 bg-background text-muted-foreground opacity-0 outline-none transition-opacity hover:border-muted-foreground hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring group-hover/ins:opacity-100",
            open && "opacity-100",
          )}
        >
          <PlusIcon className="size-3" />
        </button>
      </div>
    </DocInsertPopover>
  );
}

/** "+ Add a step" for an empty loop or branch, or the end of the doc. */
export function DocAddStep({
  place,
  label,
  emptyText,
  onDrop,
}: {
  place: DocPlace;
  label: string;
  emptyText?: string;
  // Set while a step is dragged: the row is a drop target.
  onDrop?: () => void;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const [open, setOpen] = useState(false);
  const drop = useDropTarget(onDrop);
  const addRef = useRef<HTMLButtonElement>(null);
  if (!actions.edit)
    return emptyText ? (
      <p className="my-1 text-[13px] text-muted-foreground">{emptyText}.</p>
    ) : null;
  return (
    <DocInsertPopover
      open={open}
      onOpenChange={setOpen}
      onDemonstrate={demonstrateFrom(actions, place)}
      onEscape={() => requestAnimationFrame(() => addRef.current?.focus())}
      onPick={(type) => {
        const added = actions.edit?.insertStep(place, type) ?? null;
        actions.setFocusId(added);
        if (added) announceAdded(type);
      }}
    >
      <p
        {...drop.handlers}
        className={cn(
          "my-1 flex flex-wrap items-center gap-1.5 rounded text-[13px] text-muted-foreground",
          onDrop && "-mx-2 px-2 py-1 outline-dashed outline-1 outline-border",
          drop.over && "bg-primary/10 outline-2 outline-primary",
        )}
      >
        {emptyText && <span>{emptyText} ·</span>}
        <button
          type="button"
          data-doc-add-step
          ref={addRef}
          onClick={() => setOpen(true)}
          className="rounded-md px-2 py-1 text-sm text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          + {label}
        </button>
      </p>
    </DocInsertPopover>
  );
}

function DocNote({
  ownerNodeId,
  placement,
  text,
  title,
  editable,
  place,
  nestsInto,
  into,
}: {
  ownerNodeId: string;
  placement: "above" | "trailing";
  text: string;
  title?: string;
  editable: boolean;
  place?: DocPlace;
  // What Tab would put this line in, for the placeholder to say so.
  nestsInto?: "loop" | "branch";
  // Set when Tab put this line in an empty loop or branch: the picked step goes in there, holding the line's text.
  into?: DocScopeRef;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const [inserting, setInserting] = useState(false);
  const finishSlash = useRef<Parameters<PickerOpener>[0] | null>(null);
  // The picker the insert line opened: closing it leaves the empty line with its caret, typing no "/".
  const fromInsertLine = useRef(false);
  // Escape leaves an empty line the insert line opened back on its "+", not on the page.
  const openedByInsertLine = useRef(false);
  // The Tab clause shows only where there is a Tab key and it fits on one row, so the line never grows a row.
  const lineBox = useRef<HTMLDivElement>(null);
  const tabPlaceholder =
    nestsInto && !noTabKey()
      ? `Write a note, type / for a step, or Tab to put it in the ${nestsInto}`
      : null;
  const tabFits = useFitsOneRow(lineBox, tabPlaceholder);
  const key = placement === "above" ? "notes" : "trailingNotes";
  const pending =
    actions.pendingNote?.ownerNodeId === ownerNodeId &&
    actions.pendingNote.placement === placement;
  return (
    <DocInsertPopover
      open={inserting}
      onOpenChange={setInserting}
      onDemonstrate={
        // Tab on a note under an empty loop or branch picks a step for that scope, which has no demonstration insertion.
        // The "+" between steps offers the same rows whether or not a note sits above the step: no demonstration.
        into || fromInsertLine.current
          ? null
          : demonstrateFrom(actions, place, finishSlash)
      }
      onDismiss={() => {
        // The picker can report closing more than once; only the first finishes the line.
        const finish = finishSlash.current;
        finishSlash.current = null;
        finish?.(true, !fromInsertLine.current);
        fromInsertLine.current = false;
      }}
      onPick={(type) => {
        finishSlash.current?.(false);
        fromInsertLine.current = false;
        if (into && actions.edit) {
          const adopted = actions.edit.adoptNote(
            { ownerNodeId, placement },
            into,
            type,
          );
          actions.setPendingNote(null);
          actions.setFocusId(adopted);
          if (adopted) announceAdded(type);
          return;
        }
        if (!place || !actions.edit) return;
        const added = actions.edit.insertStep(place, type);
        // "/" on an empty line turns that line into the step. The text edit goes after the insert, on the latest graph.
        if (added && text && !text.trim())
          actions.edit.setText(ownerNodeId, key, "");
        actions.setPendingNote(null);
        actions.setFocusId(added);
        if (added) announceAdded(type);
      }}
    >
      <div
        ref={lineBox}
        title={title}
        className="text-base leading-[1.55] text-muted-foreground"
      >
        <DocEditableLine
          value={text}
          editable={editable && Boolean(actions.edit)}
          multiline
          autoFocus={pending}
          placeholder={NOTE_PLACEHOLDER}
          editingPlaceholder={
            tabPlaceholder && tabFits ? tabPlaceholder : NOTE_PLACEHOLDER
          }
          label="Note"
          maxLength={NOTE_MAX_LENGTH}
          references={
            actions.edit
              ? () => actions.edit!.references(ownerNodeId, true)
              : undefined
          }
          onChange={(v) => {
            // An emptied note leaves the data; keep its line on screen as the pending one so typing continues.
            // Emptying an editable line can leave a bare line break, so blank counts as empty.
            if (!v.trim() && !pending)
              actions.setPendingNote({ ownerNodeId, placement });
            actions.edit?.setText(ownerNodeId, key, v);
          }}
          onSlash={
            place || into
              ? (keep) => {
                  finishSlash.current = keep;
                  setInserting(true);
                }
              : undefined
          }
          pickerOnStart={() => {
            // Asked once per line; a later call must not clear the mark the first one set.
            const asked = takePickerOnLine(`${ownerNodeId}:${placement}`);
            if (asked)
              fromInsertLine.current = openedByInsertLine.current = true;
            return asked;
          }}
          onDone={() => {
            if (!pending) return;
            actions.setPendingNote(null);
            if (!openedByInsertLine.current) return;
            openedByInsertLine.current = false;
            requestAnimationFrame(() => {
              if (
                document.activeElement &&
                document.activeElement !== document.body
              )
                return;
              Array.from(
                document.querySelectorAll<HTMLElement>(
                  "[data-doc-insert-line]",
                ),
              )
                .find((line) => line.dataset.docInsertLine === ownerNodeId)
                ?.querySelector("button")
                ?.focus();
            });
          }}
          onTab={
            actions.edit
              ? (outdent) => {
                  const note = { ownerNodeId, placement };
                  // The line moves on, so its blur must not send focus back to the insert line.
                  openedByInsertLine.current = false;
                  // The line only shows inside; Shift+Tab shows it back where its text is kept.
                  const edit = actions.edit!;
                  if (into) {
                    if (!outdent) return false;
                    const kind = edit.nestKind(into.parentId);
                    if (kind) announceNest(kind, true);
                    requestAnimationFrame(() => actions.setPendingNote(note));
                    return true;
                  }
                  const moved = edit.nestNote(note, outdent);
                  if (!moved) return false;
                  const kind =
                    "pickIn" in moved
                      ? edit.nestKind(moved.pickIn.parentId)
                      : edit.nestKind(moved.ownerNodeId, !outdent);
                  if ("pickIn" in moved) {
                    // Nothing is saved yet: show the empty loop or branch the line goes in, with the caret at the
                    // line's start so "/" opens the picker, and say so instead of claiming a move.
                    const { parentId, conditionalBranchId } = moved.pickIn;
                    if (
                      parentId &&
                      kind === "loop" &&
                      !actions.loopOpen(parentId)
                    )
                      actions.toggleLoop(parentId);
                    if (parentId && kind === "branch") {
                      if (!actions.conditionalOpen(parentId))
                        actions.toggleConditional(parentId);
                      if (conditionalBranchId)
                        actions.selectBranch(parentId, conditionalBranchId);
                    }
                    armNextCaretAtStart();
                    if (kind) announceNestReady(kind);
                  } else if (kind) announceNest(kind, outdent);
                  // Tab only nests: an empty loop or branch takes the line itself, and "/" then picks its step.
                  const next =
                    "pickIn" in moved ? { ...note, into: moved.pickIn } : moved;
                  // The line remounts in its new place and the old one's blur clears the pending note, so set it after.
                  requestAnimationFrame(() => actions.setPendingNote(next));
                  return true;
                }
              : undefined
          }
        />
      </div>
    </DocInsertPopover>
  );
}

export function DocScope({
  items,
  byId,
  readOnly = false,
  scope,
  emptyText,
  noteProblems = new Map(),
}: {
  items: DocItem[];
  byId: Map<string, AppNode>;
  readOnly?: boolean;
  // The sequence these items are, when it can be edited.
  scope?: DocScopeRef;
  emptyText?: string;
  noteProblems?: ReadonlyMap<string, DocNoteProblem>;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const steps = items.filter(isNumberedItem);
  // Steps shared by several branches sit in this list but belong to the branches.
  const structural =
    Boolean(actions.edit && scope) &&
    !readOnly &&
    steps.length === items.filter((i) => i.kind !== "note").length &&
    steps.every((s) => !s.readOnlyReason && !s.reachedFrom);
  const movable = steps.filter((s) => !s.finally);
  const placeOf = (nodeId: string): DocStepPlace | undefined => {
    if (!structural || !scope) return undefined;
    const index = movable.findIndex((s) => s.nodeId === nodeId);
    const step = steps.find((s) => s.nodeId === nodeId);
    return {
      scope,
      index: index === -1 ? movable.length : index,
      count: movable.length,
      isFinally: Boolean(step?.finally),
    };
  };
  const notePresent = (owner: string, placement: "above" | "trailing") =>
    items.some(
      (i) =>
        i.kind === "note" &&
        i.ownerNodeId === owner &&
        i.placement === placement,
    );
  const noteBelow = (nodeId: string) => {
    const at = steps.findIndex((s) => s.nodeId === nodeId);
    const next = steps[at + 1];
    const note = next
      ? { ownerNodeId: next.nodeId, placement: "above" as const }
      : { ownerNodeId: nodeId, placement: "trailing" as const };
    actions.setPendingNote(note);
  };
  // Enter on a step makes a step of the same type below, splitting its text at the caret. Enter on an empty step that
  // needs text turns it back into a note, as Enter leaves an empty list item. Loops, Ifs and steps that need no text
  // (Print Page, a Wait with its amount) keep the step and open an empty line below.
  const enterAfter = (
    nodeId: string,
    split: LineSplit | null,
    key: string | undefined,
  ) => {
    const node = byId.get(nodeId);
    const place = placeOf(nodeId);
    const edit = actions.edit;
    if (
      !edit ||
      !place ||
      place.isFinally ||
      !key ||
      !node ||
      !isWorkflowBlockNode(node) ||
      node.type === "loop" ||
      node.type === "conditional"
    )
      return noteBelow(nodeId);
    const needsText =
      getDocProseFields(node).length > 0 ||
      getDocMissingSettings(node).length > 0;
    if (!needsText) return noteBelow(nodeId);
    if (
      split &&
      !split.before.trim() &&
      !split.after.trim() &&
      edit.canBePlainText(nodeId)
    ) {
      const note = edit.toPlainText(nodeId);
      if (note) {
        actions.setPendingNote(note);
        return;
      }
      // Refused (a lone step, or its output is used): Enter still adds the next step.
    }
    const added = edit.splitStep(place, nodeId, split && { key, ...split });
    if (!added) return noteBelow(nodeId);
    if (split?.after.trim()) requestCaretAtStart(added);
    actions.setFocusId(added);
  };
  const pending = actions.pendingNote;
  const pendingFor = (owner: string, placement: "above" | "trailing") =>
    pending?.ownerNodeId === owner &&
    pending.placement === placement &&
    !pending.into &&
    !notePresent(owner, placement);
  // Shown inside the empty loop or branch Tab put it in, not where its text is kept.
  const shownElsewhere = (owner: string, placement: "above" | "trailing") =>
    pending?.ownerNodeId === owner &&
    pending.placement === placement &&
    Boolean(pending.into);
  const pendingInto =
    pending?.into && sameScope(pending.into, scope) ? pending : null;
  // The loop or If that Tab would put a line in: the one it trails, or the step just above it.
  const nestsInto = (owner: string, placement: "above" | "trailing") => {
    const container =
      placement === "trailing"
        ? byId.get(owner)
        : byId.get(
            steps[steps.findIndex((s) => s.nodeId === owner) - 1]?.nodeId ?? "",
          );
    if (container?.type === "loop") return "loop" as const;
    if (container?.type === "conditional") return "branch" as const;
    return undefined;
  };
  const editable = !readOnly && Boolean(actions.edit);
  // A dragged container can't be dropped inside itself, so its own scopes offer no drop targets.
  const dragInside = (() => {
    for (
      let cur = byId.get(scope?.parentId ?? "");
      cur;
      cur = byId.get(cur.parentId ?? "")
    )
      if (cur.id === actions.dragId) return true;
    return false;
  })();
  const dropping = Boolean(
    actions.dragId && structural && scope && actions.edit && !dragInside,
  );
  const dropAt = (index: number) => () => {
    const id = actions.dragId;
    actions.setDragId(null);
    if (!id || !scope || !actions.edit) return;
    // The index counts the dragged step while it is still in place.
    const from = movable.findIndex((s) => s.nodeId === id);
    const to = from !== -1 && from < index ? index - 1 : index;
    if (!actions.edit.move(id, { scope, index: to })) return;
    // Within its own list the step went up or down; only a step that came from elsewhere reads out the list's name.
    if (from !== -1) {
      if (to !== from) announceMoved(to < from ? "up" : "down");
      return;
    }
    const place = actions
      .destinations()
      .find(
        (d) =>
          d.scope.parentId === scope.parentId &&
          d.scope.conditionalBranchId === scope.conditionalBranchId,
      )?.label;
    if (place) announceMoved({ place });
  };
  // The drop line before a step goes above its note, which belongs to it.
  const zoneBefore = (index: number) => {
    if (!dropping) return null;
    const item = items[index]!;
    const owner =
      item.kind === "note"
        ? item.placement === "above"
          ? item.ownerNodeId
          : null
        : item.kind === "unplaced"
          ? null
          : item.nodeId;
    if (!owner) return null;
    const previous = items[index - 1];
    if (
      item.kind !== "note" &&
      previous?.kind === "note" &&
      previous.placement === "above" &&
      previous.ownerNodeId === owner
    )
      return null;
    const place = placeOf(owner);
    return place ? <DocDropZone onDrop={dropAt(place.index)} /> : null;
  };
  // The insert line above a step goes above its note too, as the drop line does. The doc's first step has none:
  // the top of the doc is not between steps.
  const insertBefore = (index: number) => {
    if (dropping || actions.runGoing || !structural || !editable || !scope)
      return null;
    const item = items[index]!;
    const owner =
      item.kind === "note"
        ? item.placement === "above"
          ? item.ownerNodeId
          : null
        : item.kind === "unplaced"
          ? null
          : item.nodeId;
    if (!owner) return null;
    const previous = items[index - 1];
    if (
      item.kind !== "note" &&
      previous?.kind === "note" &&
      previous.placement === "above" &&
      previous.ownerNodeId === owner
    )
      return null;
    const place = placeOf(owner);
    if (!place || (sameScope(scope, TOP_SCOPE) && place.index === 0))
      return null;
    // A new step can't go between two steps that always run at the end, so only the first of them gets a line.
    if (
      place.isFinally &&
      steps.findIndex((s) => s.finally) !==
        steps.findIndex((s) => s.nodeId === owner)
    )
      return null;
    const first = place.index === 0;
    return (
      <DocInsertLine
        place={{ scope: place.scope, index: place.index }}
        ownerNodeId={owner}
        noteFree={!notePresent(owner, "above") && !pendingFor(owner, "above")}
        label={
          first
            ? `Add a step at the start of this ${scope.conditionalBranchId ? "branch" : "loop"}`
            : "Add a step here"
        }
      />
    );
  };
  const hasFinally = steps.some((s) => s.finally);
  const demonstration = useRecordingLauncherStore((s) => s.demonstration);
  const lineAt =
    demonstration && scope && !scope.parentId && !scope.conditionalBranchId
      ? demonstrationLineIndex(
          items,
          (id) => {
            const node = byId.get(id);
            return node && isWorkflowBlockNode(node)
              ? node.data.label
              : undefined;
          },
          demonstration.insertion,
        )
      : -1;
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-3",
        // Inside a loop or branch the first step is as far below the header as steps are from each other, so its "+" covers no header text.
        scope && items.length ? "pb-[3px] pt-3" : "py-[3px]",
      )}
    >
      {items.map((item, index) => (
        // Same key with or without a drop line, so starting a drag never remounts the dragged step.
        <Fragment
          key={
            item.kind === "note"
              ? `note-${item.ownerNodeId}-${item.placement}`
              : item.kind === "unplaced"
                ? `unplaced-${item.nodeIds[0]}`
                : item.nodeId
          }
        >
          {lineAt === index && demonstration && (
            <DocDemonstrationLine status={demonstration} />
          )}
          {zoneBefore(index)}
          {insertBefore(index)}
          {renderItem(item, index)}
        </Fragment>
      ))}
      {lineAt === items.length && demonstration && (
        <DocDemonstrationLine status={demonstration} />
      )}
      {dropping && !hasFinally && steps.length > 0 && (
        <DocDropZone onDrop={dropAt(movable.length)} />
      )}
      {steps.length === 0 && pendingInto && scope && (
        <DocNote
          ownerNodeId={pendingInto.ownerNodeId}
          placement={pendingInto.placement}
          text={noteTextOfNode(byId, pendingInto)}
          editable={editable}
          into={scope}
        />
      )}
      {steps.length === 0 &&
        scope &&
        scope !== TOP_SCOPE &&
        (emptyText || structural) && (
          <DocAddStep
            place={{ scope, index: 0 }}
            label="Add a step"
            emptyText={emptyText}
            onDrop={dropping ? dropAt(0) : undefined}
          />
        )}
    </div>
  );

  function renderItem(item: DocItem, index: number) {
    if (item.kind === "note") {
      if (shownElsewhere(item.ownerNodeId, item.placement)) return null;
      const ownerPlace = placeOf(item.ownerNodeId);
      const problem = noteProblems.get(`${item.ownerNodeId}:${item.placement}`);
      return (
        <div
          key={`note-${item.ownerNodeId}-${item.placement}`}
          data-doc-note-problem={problem?.code}
        >
          <DocProposalNoteMark item={item} byId={byId}>
            <DocNote
              ownerNodeId={item.ownerNodeId}
              placement={item.placement}
              text={item.text}
              title={noteReaders(item, byId, actions.previousNoteReader)}
              nestsInto={nestsInto(item.ownerNodeId, item.placement)}
              editable={editable && !previousReason(items, index + 1)}
              place={
                ownerPlace && {
                  scope: ownerPlace.scope,
                  index:
                    ownerPlace.index + (item.placement === "trailing" ? 1 : 0),
                }
              }
            />
          </DocProposalNoteMark>
          {problem && (
            <p className="mt-1 text-sm font-medium text-amber-700 dark:text-amber-300">
              {problem.message}
            </p>
          )}
        </div>
      );
    }
    if (item.kind === "unplaced")
      return (
        <DocReadOnly
          key={`unplaced-${item.nodeIds[0]}`}
          reason={item.readOnlyReason ?? ""}
        >
          <div className="mt-2 flex flex-col gap-2">
            {item.nodeIds.map((id) => {
              const node = byId.get(id);
              return node && isWorkflowBlockNode(node) ? (
                <DocProposalStepMark key={id} node={node}>
                  <div data-doc-node-id={id}>
                    <DocStepHeader node={node} editable={false} />
                    <DocStepBody
                      node={node}
                      hasFix={noteProblems.has(`${id}:fix`)}
                    />
                    <DocStepProblem
                      nodeId={id}
                      problems={noteProblems}
                      byId={byId}
                      editable={false}
                    />
                    <DocStepResult label={node.data.label} />
                    {node.type === "loop" && (
                      <DocProposalLoopRemovals loopLabel={node.data.label} />
                    )}
                  </div>
                </DocProposalStepMark>
              ) : null;
            })}
          </div>
        </DocReadOnly>
      );
    return (
      <div key={item.nodeId} className="min-w-0">
        {item.finally && (
          <div className="mb-1.5 mt-[18px] flex items-center gap-2.5 text-[12.5px] text-muted-foreground before:flex-1 before:border-t before:border-dashed before:border-border after:flex-1 after:border-t after:border-dashed after:border-border">
            Always runs at the end, even if the run fails or stops
          </div>
        )}
        {item.readOnlyReason &&
          !readOnly &&
          item.readOnlyReason !== previousReason(items, index) && (
            <DocReadOnly reason={item.readOnlyReason} />
          )}
        {pendingFor(item.nodeId, "above") && (
          <div className="mb-3">
            <DocNote
              ownerNodeId={item.nodeId}
              placement="above"
              text=""
              editable={editable}
              nestsInto={nestsInto(item.nodeId, "above")}
              place={placeOf(item.nodeId)}
            />
          </div>
        )}
        <DocProposalStepMark node={byId.get(item.nodeId)}>
          <div data-doc-node-id={item.nodeId} tabIndex={-1}>
            <DocNode
              item={item}
              byId={byId}
              readOnly={readOnly}
              place={placeOf(item.nodeId)}
              // A numbering mismatch leaves the step unnumbered rather than breaking the render.
              number={actions.stepNumbers.get(item.nodeId) ?? ""}
              onEnter={
                editable
                  ? (split, key) => enterAfter(item.nodeId, split, key)
                  : undefined
              }
              noteProblems={noteProblems}
            />
          </div>
        </DocProposalStepMark>
        {pendingFor(item.nodeId, "trailing") && (
          <div className="mt-3">
            <DocNote
              ownerNodeId={item.nodeId}
              placement="trailing"
              text=""
              editable={editable}
              nestsInto={nestsInto(item.nodeId, "trailing")}
              place={(() => {
                const p = placeOf(item.nodeId);
                return p && { scope: p.scope, index: p.index + 1 };
              })()}
            />
          </div>
        )}
      </div>
    );
  }
}
