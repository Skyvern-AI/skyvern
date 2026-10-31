import { useContext, useRef, useState, type ReactNode } from "react";
import { ChevronRightIcon, GearIcon } from "@radix-ui/react-icons";
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
import type { WorkflowBlockNode } from "../nodes";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../nodes/types";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { getDocSettingsChips } from "./docSettingsChips";
import {
  docBlockType,
  docTagClasses,
  docTagColor,
  getDocKeyFields,
  getDocProseFields,
  getDocSettingSummary,
  getDocStepSummary,
  docStepCanRun,
} from "./docStepRegistry";
import { DocRunStepButton } from "./DocRunStepButton";
import { DocText } from "./DocText";
import type { DocPlace } from "./docEditApi";
import {
  DocDeleteConfirm,
  DocEditableLine,
  DocMoveToDialog,
  DocOutputName,
  DocStepPicker,
} from "./DocEditing";

/** Where a step sits, for the moves its tag menu offers. Absent when the step's place can't be edited. */
export type DocStepPlace = DocPlace & { count: number; isFinally: boolean };

const tagClass = (blockType: ReturnType<typeof docBlockType>) =>
  cn(
    "inline-flex h-[21px] shrink-0 items-center gap-1 whitespace-nowrap rounded pl-[5px] pr-[7px] text-[12.5px] font-semibold",
    docTagClasses[docTagColor(blockType)],
  );

function DocStepMenu({
  node,
  place,
}: {
  node: WorkflowBlockNode;
  place: DocStepPlace;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const edit = actions.edit!;
  const [moveOpen, setMoveOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const tagRef = useRef<HTMLButtonElement>(null);
  const [confirming, setConfirming] = useState(false);
  const blockType = docBlockType(node);
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
  const remove = () => {
    if (actions.outputUsers(node.id).length) setConfirming(true);
    else edit.remove(node.id);
  };
  return (
    <>
      <DocDeleteConfirm
        id={confirming ? node.id : null}
        onClose={() => setConfirming(false)}
      >
        <span className="relative inline-flex">
          <button
            type="button"
            ref={tagRef}
            // The tag is also the drag handle, so the menu opens on click: a menu that opens on press cancels the drag.
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setMenuOpen(true);
              }
            }}
            draggable={!place.isFinally}
            onDragStart={(event) => {
              setMenuOpen(false);
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("text/plain", node.data.label);
              actions.setDragId(node.id);
            }}
            onDragEnd={() => actions.setDragId(null)}
            title={
              place.isFinally ? undefined : "Click for actions, drag to move"
            }
            aria-label={`${blockType ? workflowBlockTitle[blockType] : node.type} step ${node.data.label}, actions`}
            className={cn(
              tagClass(blockType),
              "outline-none hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            {blockType && (
              <WorkflowBlockIcon
                workflowBlockType={blockType}
                className="size-3"
              />
            )}
            {blockType ? workflowBlockTitle[blockType] : node.type}
          </button>
          <DropdownMenu
            modal={false}
            open={menuOpen}
            onOpenChange={setMenuOpen}
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
                // A click on the tag toggles the menu itself.
                if (tagRef.current?.contains(event.target as Element))
                  event.preventDefault();
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                if (
                  document.activeElement === document.body ||
                  !document.activeElement
                )
                  tagRef.current?.focus();
              }}
            >
              {!place.isFinally && (
                <DropdownMenuItem
                  onSelect={() => setTimeout(() => edit.duplicate(node.id), 0)}
                >
                  Duplicate
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onSelect={() => actions.openSettings(node.id)}>
                Settings
              </DropdownMenuItem>
              {topLevel && !container && (
                <DropdownMenuItem
                  onSelect={() => edit.setFinally(node.id, !place.isFinally)}
                >
                  {place.isFinally
                    ? "Don't always run at the end"
                    : "Always run at the end"}
                </DropdownMenuItem>
              )}
              {!container && (
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
                          setMenuOpen(false);
                          setTimeout(() => edit.turnInto(node.id, type), 0);
                        }}
                      />
                    </DropdownMenuSubContent>
                  </DropdownMenuPortal>
                </DropdownMenuSub>
              )}
              {!container && edit.canBePlainText(node.id) && (
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
              {!place.isFinally && (
                <>
                  <DropdownMenuSeparator />
                  {place.index > 0 && (
                    <DropdownMenuItem
                      onSelect={() =>
                        edit.move(node.id, {
                          scope: place.scope,
                          index: place.index - 1,
                        })
                      }
                    >
                      Move up
                    </DropdownMenuItem>
                  )}
                  {place.index < place.count - 1 && (
                    <DropdownMenuItem
                      onSelect={() =>
                        edit.move(node.id, {
                          scope: place.scope,
                          index: place.index + 1,
                        })
                      }
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
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                onSelect={() => setTimeout(remove, 0)}
              >
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </span>
      </DocDeleteConfirm>
      <DocMoveToDialog
        open={moveOpen}
        onOpenChange={setMoveOpen}
        destinations={destinations}
        onPick={(d) => edit.move(node.id, d)}
      />
    </>
  );
}

export function DocTag({
  node,
  place,
}: {
  node: WorkflowBlockNode;
  place?: DocStepPlace;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const blockType = docBlockType(node);
  if (actions.edit && place) return <DocStepMenu node={node} place={place} />;
  return (
    <span className={tagClass(blockType)}>
      {blockType && (
        <WorkflowBlockIcon workflowBlockType={blockType} className="size-3" />
      )}
      {blockType ? workflowBlockTitle[blockType] : node.type}
    </span>
  );
}

export function DocSettingsButton({
  node,
  editable,
  inline = false,
}: {
  node: WorkflowBlockNode;
  editable: boolean;
  inline?: boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  if (!editable) return null;
  const changed = getDocSettingsChips(node).length;
  const button = (
    <button
      type="button"
      aria-label={`Settings for ${node.data.label}${changed ? `, ${changed} changed` : ""}`}
      title={
        changed
          ? `${changed} setting${changed > 1 ? "s" : ""} changed`
          : "Settings"
      }
      onClick={() => actions.openSettings(node.id)}
      className={cn(
        "inline-flex items-center gap-[3px] rounded-[5px] py-0.5 text-xs font-semibold leading-4 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring",
        changed
          ? "bg-muted pl-1 pr-1.5 text-foreground"
          : "px-1 opacity-0 group-focus-within/line:opacity-100 group-hover/line:opacity-100",
      )}
    >
      <GearIcon className="size-[15px]" />
      {changed > 0 && <span>{changed}</span>}
    </button>
  );
  if (changed || inline) return button;
  // Shown only on hover or focus, so it takes no width: on a narrow pane it must not wrap the row onto a blank line.
  return (
    <span className="relative -ml-2 h-5 w-0 shrink-0 self-center">
      <span className="absolute left-2 top-0">{button}</span>
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
          className="absolute -left-[26px] top-0.5 grid h-[22px] w-[22px] place-items-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            className={cn(
              "size-4 transition-transform",
              disclosure.open && "rotate-90",
            )}
          />
        </button>
      )}
      <DocTag node={node} place={editable ? place : undefined} />
      {extra}
      {node.type !== "conditional" && (
        <span className="flex min-w-[7rem] flex-1 items-center gap-1 text-[12.5px] text-muted-foreground">
          saves{" "}
          {editable && place ? (
            <DocOutputName id={node.id} label={node.data.label} />
          ) : (
            <span
              title={`${node.data.label}_output`}
              className="min-w-0 truncate rounded bg-muted px-[5px] font-medium leading-[1.25]"
            >
              {node.data.label}_output
            </span>
          )}
        </span>
      )}
      {/* With a Run button the row already reserves width, so the gear takes its own slot instead of overlapping Run. */}
      <DocSettingsButton
        node={node}
        editable={editable}
        inline={editable && docStepCanRun(node)}
      />
      {showRunControl && <DocRunStepButton node={node} disabled={!editable} />}
    </div>
  );
}

export function DocStepBody({
  node,
  editable = false,
  onEnter,
  onSlash,
  onTab,
  onBackspaceAtStart,
}: {
  node: WorkflowBlockNode;
  editable?: boolean;
  onEnter?: () => void;
  onSlash?: (finish: (keepSlash: boolean) => void) => void;
  onTab?: (outdent: boolean) => boolean;
  onBackspaceAtStart?: () => boolean;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const data = node.data as Record<string, unknown>;
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
  const keyFields = isProse ? [] : getDocKeyFields(node);
  // A description sits under a prompt-driven step's text; on other steps it is the main line, with the setting it would hide underneath.
  const settingSummary =
    !isProse && description ? getDocSettingSummary(node) : null;
  const focus = actions.focusId === node.id;
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
      multiline
      onChange={(v) => {
        if (!v.trim() && !kept.includes(key)) setKept([...kept, key]);
        actions.edit?.setText(node.id, key, v);
      }}
      onEnter={onEnter}
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
            line(main.key, main.value, "No instructions yet", "")
          ) : (
            <span className="font-normal text-muted-foreground">
              No instructions yet
            </span>
          )
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
