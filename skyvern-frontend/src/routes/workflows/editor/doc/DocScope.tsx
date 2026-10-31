import {
  Fragment,
  useContext,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";
import { cn } from "@/util/utils";
import { LockClosedIcon } from "@radix-ui/react-icons";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { workflowBlockTitle } from "../nodes/types";
import { countDocSteps, type DocItem, type DocNoteItem } from "./buildDocTree";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { docBlockType } from "./docStepRegistry";
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

type PlacedItem = Exclude<DocItem, DocNoteItem>;
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
}: {
  nodeId: string;
  problems: ReadonlyMap<string, DocNoteProblem>;
}) {
  const problem = problems.get(`${nodeId}:step`);
  return problem ? (
    <p
      data-doc-step-problem
      className="mt-1 whitespace-pre-line text-sm font-medium text-amber-700 dark:text-amber-300"
    >
      {problem.message}
    </p>
  ) : null;
}

function DocNode({
  item,
  byId,
  readOnly,
  place,
  onEnter,
  noteProblems,
}: {
  item: Exclude<PlacedItem, { kind: "unplaced" }>;
  byId: Map<string, AppNode>;
  readOnly: boolean;
  place?: DocStepPlace;
  onEnter?: () => void;
  noteProblems: ReadonlyMap<string, DocNoteProblem>;
}) {
  const [inserting, setInserting] = useState(false);
  const finishSlash = useRef<((keepSlash: boolean) => void) | null>(null);
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
        <DocStepProblem nodeId={item.nodeId} problems={noteProblems} />
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
          onPick={(type) => {
            finishSlash.current?.(false);
            if (!place || !actions.edit) return;
            actions.setFocusId(
              actions.edit.insertStep(
                { scope: place.scope, index: place.index + 1 },
                type,
              ),
            );
          }}
        >
          <div>
            <DocStepBody
              node={node}
              editable={editable && Boolean(actions.edit)}
              onEnter={onEnter}
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
                      const nested = actions.edit!.nest(node.id, outdent);
                      if (nested) actions.setFocusId(node.id);
                      return nested;
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
        <DocStepProblem nodeId={item.nodeId} problems={noteProblems} />
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
  if (!actions.edit)
    return emptyText ? (
      <p className="my-1 text-[13px] text-muted-foreground">{emptyText}.</p>
    ) : null;
  return (
    <DocInsertPopover
      open={open}
      onOpenChange={setOpen}
      onPick={(type) =>
        actions.setFocusId(actions.edit?.insertStep(place, type) ?? null)
      }
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
          onClick={() => setOpen(true)}
          className="rounded text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
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
}: {
  ownerNodeId: string;
  placement: "above" | "trailing";
  text: string;
  title?: string;
  editable: boolean;
  place?: DocPlace;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const [inserting, setInserting] = useState(false);
  const finishSlash = useRef<((keepSlash: boolean) => void) | null>(null);
  const key = placement === "above" ? "notes" : "trailingNotes";
  const pending =
    actions.pendingNote?.ownerNodeId === ownerNodeId &&
    actions.pendingNote.placement === placement;
  return (
    <DocInsertPopover
      open={inserting}
      onOpenChange={setInserting}
      onDismiss={() => finishSlash.current?.(true)}
      onPick={(type) => {
        finishSlash.current?.(false);
        if (!place || !actions.edit) return;
        const added = actions.edit.insertStep(place, type);
        // "/" on an empty line turns that line into the step. The text edit goes after the insert, on the latest graph.
        if (added && text && !text.trim())
          actions.edit.setText(ownerNodeId, key, "");
        actions.setPendingNote(null);
        actions.setFocusId(added);
      }}
    >
      <div
        title={title}
        className="text-base leading-[1.55] text-muted-foreground"
      >
        <DocEditableLine
          value={text}
          editable={editable && Boolean(actions.edit)}
          multiline
          autoFocus={pending}
          placeholder="Write a note, or type / for a step"
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
            place
              ? (keep) => {
                  finishSlash.current = keep;
                  setInserting(true);
                }
              : undefined
          }
          onDone={() => pending && actions.setPendingNote(null)}
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
  const steps = items.filter(
    (i): i is Exclude<PlacedItem, { kind: "unplaced" }> =>
      i.kind !== "note" && i.kind !== "unplaced",
  );
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
  // Enter at the end of a step starts the note under it: the next step's note, or this one's trailing note.
  const enterAfter = (nodeId: string) => {
    const at = steps.findIndex((s) => s.nodeId === nodeId);
    const next = steps[at + 1];
    const note = next
      ? { ownerNodeId: next.nodeId, placement: "above" as const }
      : { ownerNodeId: nodeId, placement: "trailing" as const };
    actions.setPendingNote(note);
  };
  const pendingFor = (owner: string, placement: "above" | "trailing") =>
    actions.pendingNote?.ownerNodeId === owner &&
    actions.pendingNote.placement === placement &&
    !notePresent(owner, placement);
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
    actions.edit.move(id, {
      scope,
      index: from !== -1 && from < index ? index - 1 : index,
    });
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
  const hasFinally = steps.some((s) => s.finally);
  return (
    <div className="flex min-w-0 flex-col gap-3 py-[3px]">
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
          {zoneBefore(index)}
          {renderItem(item, index)}
        </Fragment>
      ))}
      {dropping && !hasFinally && steps.length > 0 && (
        <DocDropZone onDrop={dropAt(movable.length)} />
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
                    <DocStepBody node={node} />
                    <DocStepProblem nodeId={id} problems={noteProblems} />
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
              onEnter={editable ? () => enterAfter(item.nodeId) : undefined}
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
