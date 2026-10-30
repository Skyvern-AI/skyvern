import { useContext, type ReactNode } from "react";
import { LockClosedIcon } from "@radix-ui/react-icons";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { workflowBlockTitle } from "../nodes/types";
import { countDocSteps, type DocItem, type DocNoteItem } from "./buildDocTree";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { docBlockType } from "./docStepRegistry";
import { DocConditional } from "./DocConditional";
import { DocStepBody, DocStepHeader } from "./DocStep";
import { DocText } from "./DocText";

type PlacedItem = Exclude<DocItem, DocNoteItem>;

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
  items: DocItem[],
  index: number,
  byId: Map<string, AppNode>,
): string {
  const note = items[index] as DocNoteItem;
  const placed = (i: number) =>
    items[i]?.kind !== "note"
      ? (items[i] as PlacedItem | undefined)
      : undefined;
  const owner = stepName(byId.get(note.ownerNodeId));
  if (note.placement === "trailing")
    return `The agent reads this in the ${owner} step above.`;
  let previous: PlacedItem | undefined;
  for (let i = index - 1; i >= 0 && !previous; i--) previous = placed(i);
  const above =
    previous && previous.kind !== "unplaced"
      ? stepName(byId.get(previous.nodeId))
      : null;
  return above
    ? `The agent reads this in the ${above} step above and the ${owner} step below.`
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

function DocNode({
  item,
  byId,
  readOnly,
}: {
  item: Exclude<PlacedItem, { kind: "unplaced" }>;
  byId: Map<string, AppNode>;
  readOnly: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const open = actions.loopOpen(item.nodeId);
  const node = byId.get(item.nodeId);
  if (!node || !isWorkflowBlockNode(node)) return null;
  const editable =
    actions.editable && !readOnly && !item.readOnlyReason && node.data.editable;
  const reached = item.reachedFrom ? (
    <span className="text-[12.5px] text-muted-foreground">
      reached from {item.reachedFrom} branches
    </span>
  ) : null;
  if (item.kind === "conditional")
    return (
      <DocConditional
        node={node}
        item={item}
        byId={byId}
        editable={editable}
        renderChildren={(children) => (
          <DocScope
            items={children}
            byId={byId}
            readOnly={readOnly || Boolean(item.readOnlyReason)}
          />
        )}
      />
    );
  const inside = item.kind === "loop" ? countDocSteps(item.children) : 0;
  return (
    <>
      <div className="group/line rounded py-[3px]">
        <DocStepHeader
          node={node}
          editable={editable}
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
        <DocStepBody node={node} />
      </div>
      {item.kind === "loop" && open && (
        <div className="mb-1 ml-[11px] mt-0.5 border-l border-border pl-4">
          <DocScope
            items={item.children}
            byId={byId}
            readOnly={readOnly || Boolean(item.readOnlyReason)}
          />
        </div>
      )}
    </>
  );
}

export function DocScope({
  items,
  byId,
  readOnly = false,
}: {
  items: DocItem[];
  byId: Map<string, AppNode>;
  readOnly?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-3 py-[3px]">
      {items.map((item, index) => {
        if (item.kind === "note")
          return (
            <p
              key={`note-${item.ownerNodeId}-${item.placement}`}
              title={noteReaders(items, index, byId)}
              className="text-base leading-[1.55] text-muted-foreground"
            >
              <DocText value={item.text} />
            </p>
          );
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
                    <div key={id} data-doc-node-id={id}>
                      <DocStepHeader node={node} editable={false} />
                      <DocStepBody node={node} />
                    </div>
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
            <div data-doc-node-id={item.nodeId} tabIndex={-1}>
              <DocNode item={item} byId={byId} readOnly={readOnly} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
