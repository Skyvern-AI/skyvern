import {
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ResetIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { caretOnEdge, splitAtCaret } from "./docCaret";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useIntegrationCatalogQuery } from "@/hooks/useIntegrationCatalogQuery";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { AGENT_BLOCK_FLAG } from "@/util/featureFlags";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import { cn } from "@/util/utils";
import { useSaveWorkflow } from "../hooks/useSaveWorkflow";
import { PendingGoalChangesDialog } from "../PendingGoalChangesDialog";
import { useUndoRedoShortcutLabels } from "../controls/useUndoRedoShortcutLabels";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import type { DocDestination, NodeType } from "./docEditApi";
import type { DocReferenceGroup } from "./docGraphEdits";
import { docStepChoices } from "./docStepChoices";
import { docStepFilter } from "./docStepRegistry";
import {
  DOC_RUN_AGENT_EVENT,
  armNextCaretAtStart,
  rememberReferenceLine,
  takeNextCaretAtStart,
  type LineSplit,
  type PickerOpener,
} from "./docKeys";
import { DocText } from "./DocText";
import { describeSaveFailure, releaseUnansweredSave } from "./docSaveState";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

// Menus anchored to a Doc line stay between the Doc pane's top and bottom and inside the window, cap their
// height to the room left and scroll, so they never run past the window or over the studio header.
const IN_DOC = '[role="region"][aria-label="Workflow document"]';
const docMenuClass =
  "flex max-h-[var(--radix-popover-content-available-height)] flex-col";
function useDocBoundary() {
  const [boundary, setBoundary] = useState<Element | null>(null);
  const anchorRef = useCallback(
    (el: Element | null) => setBoundary(el?.closest(IN_DOC) ?? null),
    [],
  );
  // The window is the side boundary: on a phone the studio's panes can run past it.
  const padding = () => {
    const rect = boundary?.getBoundingClientRect();
    return {
      top: Math.max(8, (rect?.top ?? 0) + 8),
      bottom: Math.max(8, window.innerHeight - (rect?.bottom ?? 0) + 8),
      left: 8,
      right: 8,
    };
  };
  return { anchorRef, padding };
}

/** The "Add a step…" list, also used by "Turn into…". */
export function DocStepPicker({
  onPick,
  exclude = [],
  placeholder = "Add a step…",
}: {
  onPick: (nodeType: NodeType) => void;
  exclude?: string[];
  placeholder?: string;
}) {
  const agentBlockEnabled = useFeatureFlag(AGENT_BLOCK_FLAG) === true;
  const { data: integrationCatalog } = useIntegrationCatalogQuery();
  const [search, setSearch] = useState("");
  const choices = useMemo(
    () => docStepChoices({ agentBlockEnabled, integrationCatalog }),
    [agentBlockEnabled, integrationCatalog],
  );
  // cmdk ranks items inside a group but never moves a group, so the group holding the best match is put first here.
  const groups = useMemo(() => {
    if (!search.trim()) return choices;
    const best = (group: (typeof choices)[number]) =>
      Math.max(
        0,
        ...group.items.map((item) =>
          docStepFilter(item.title, search, [item.description, item.nodeType]),
        ),
      );
    return [...choices].sort((a, b) => best(b) - best(a));
  }, [choices, search]);
  return (
    <Command loop filter={docStepFilter} className="min-h-0">
      <CommandInput
        autoFocus
        placeholder={placeholder}
        value={search}
        onValueChange={setSearch}
      />
      <CommandList className="max-h-[min(340px,50vh)]">
        <CommandEmpty>No step matches.</CommandEmpty>
        {groups.map((group) => (
          <CommandGroup key={group.name} heading={group.name}>
            {group.items
              .filter((item) => !exclude.includes(item.nodeType))
              .map((item) => (
                <CommandItem
                  key={item.nodeType}
                  value={item.title}
                  keywords={[item.description, item.nodeType]}
                  onSelect={() => onPick(item.nodeType)}
                  className="gap-2.5"
                >
                  <span className="grid size-7 shrink-0 place-items-center rounded-md border border-border">
                    {item.blockType && (
                      <WorkflowBlockIcon
                        workflowBlockType={item.blockType}
                        className="size-4"
                      />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm leading-tight">
                      {item.title.replace(/ Block$/, "")}
                    </span>
                    <span className="block text-xs leading-tight text-muted-foreground">
                      {item.description}
                    </span>
                  </span>
                </CommandItem>
              ))}
          </CommandGroup>
        ))}
      </CommandList>
    </Command>
  );
}

/** A popover anchored to `children` that holds the step picker. */
export function DocInsertPopover({
  open,
  onOpenChange,
  onPick,
  onDismiss,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (nodeType: NodeType) => void;
  // Closed without picking a step.
  onDismiss?: () => void;
  children: ReactNode;
}) {
  const { anchorRef, padding } = useDocBoundary();
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) onDismiss?.();
      }}
    >
      <PopoverAnchor asChild ref={anchorRef}>
        {children}
      </PopoverAnchor>
      <PopoverContent
        align="start"
        collisionPadding={padding()}
        sticky="always"
        className={cn("w-[min(320px,calc(100vw-32px))] p-0", docMenuClass)}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DocStepPicker
          onPick={(type) => {
            onOpenChange(false);
            onPick(type);
          }}
        />
      </PopoverContent>
    </Popover>
  );
}

const runHistory = (redo: boolean) => {
  const history = useWorkflowHistoryAccessStore.getState();
  // undo() flushes a pending debounced edit itself, so don't gate on canUndo here.
  if (redo) history.redo();
  else history.undo();
};

/**
 * One line of doc text. Shows references as pills; click or Enter edits the raw text.
 * Undo inside the line goes to the shared workflow history, not the browser's text undo.
 */
export function DocEditableLine({
  value,
  editable,
  placeholder,
  className,
  autoFocus = false,
  multiline = false,
  label,
  onChange,
  onEnter,
  onSlash,
  onDone,
  references,
  onTab,
  onBackspaceAtStart,
  onMove,
  onSettings,
  onRunStep,
  caretAtStart,
  maxLength,
}: {
  value: string;
  editable: boolean;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  multiline?: boolean;
  label: string;
  onChange: (value: string) => void;
  // Enter, with the text on each side of the caret.
  onEnter?: (split: LineSplit | null) => void;
  // Opens the step picker. The line stays open until finish: true types the "/" after all, false closes the line.
  onSlash?: PickerOpener;
  onDone?: (value: string) => void;
  // What "@" offers here; no "@" menu without it.
  references?: () => DocReferenceGroup[];
  // Both return false when they did nothing, so the key keeps its usual meaning. Tab can instead ask for the step picker.
  onTab?: (outdent: boolean) => boolean | PickerOpener;
  onBackspaceAtStart?: () => boolean;
  // Moves the step this line belongs to up (-1) or down (1).
  onMove?: (delta: -1 | 1) => boolean;
  onSettings?: () => void;
  onRunStep?: () => void;
  // True when entering the line should put the caret at its start.
  caretAtStart?: () => boolean;
  maxLength?: number;
}) {
  const [editing, setEditing] = useState(autoFocus);
  const [overLimit, setOverLimit] = useState(false);
  const { anchorRef, padding } = useDocBoundary();
  const ref = useRef<HTMLSpanElement>(null);
  const emitted = useRef(value);
  const [referenceGroups, setReferenceGroups] = useState<
    DocReferenceGroup[] | null
  >(null);
  const [referenceQuery, setReferenceQuery] = useState("");
  const { createInput } = useContext(WorkflowDocActionsContext);
  const typedKey = referenceQuery.trim();
  const newInputKey =
    createInput &&
    /^[A-Za-z_][A-Za-z0-9_]*$/.test(typedKey) &&
    !(referenceGroups ?? []).some((group) =>
      group.items.some((item) => item.key === typedKey),
    )
      ? typedKey
      : null;
  // The caret when "@" was typed; the menu takes focus, so the line keeps editing until it closes.
  const caret = useRef<Range | null>(null);
  // The caret when "/" opened the step picker, which also takes focus.
  const slashAt = useRef<Range | null>(null);
  const closeReferences = (insert: string) => {
    // Refocusing the line counts as leaving the menu, which calls this again; only the first call inserts.
    const range = caret.current;
    if (!range) return;
    caret.current = null;
    setReferenceGroups(null);
    const el = ref.current;
    if (!el) return;
    el.focus();
    const selection = window.getSelection();
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
    // insertText fires the input event, so the change reaches onChange and the browser's caret moves past it.
    document.execCommand("insertText", false, insert);
    const label = el.getAttribute("aria-label");
    const same = Array.from(
      el.closest(IN_DOC)?.querySelectorAll<HTMLElement>("[data-doc-line]") ??
        [],
    ).filter((line) => line.getAttribute("aria-label") === label);
    rememberReferenceLine(label ? { label, index: same.indexOf(el) } : null);
  };
  useLayoutEffect(() => {
    const el = ref.current;
    if (!editing || !el) return;
    // Outside changes (undo, rename) replace the text; the person's own typing doesn't. A stored value
    // that only lost a trailing space is still the typing: resetting would drop the space and the caret.
    const typed: string | undefined = el.innerText;
    if (
      (typed === undefined || typed.trimEnd() !== value.trimEnd()) &&
      emitted.current !== value
    )
      el.innerText = value;
    emitted.current = value;
  }, [value, editing]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!editing || !el) return;
    el.innerText = value;
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const atStart = caretAtStart?.() ?? false;
    range.collapse(takeNextCaretAtStart() || atStart);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    // Only on entering edit mode.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);
  useEffect(() => {
    if (autoFocus) setEditing(true);
  }, [autoFocus]);

  // Distinct keys: the editing span holds browser-owned text React must not reuse.
  if (!editable)
    return (
      <span key="read" className={className}>
        {value.trim() ? (
          <DocText value={value} />
        ) : (
          <DocText
            value={placeholder ?? ""}
            className="font-normal text-muted-foreground"
          />
        )}
      </span>
    );
  if (!editing)
    return (
      <span
        key="view"
        data-doc-line
        role="textbox"
        tabIndex={0}
        aria-label={label}
        onClick={() => setEditing(true)}
        onFocus={() => setEditing(true)}
        className={cn("block cursor-text rounded outline-none", className)}
      >
        {value.trim() ? (
          <DocText value={value} />
        ) : (
          <DocText
            value={placeholder ?? ""}
            className="font-normal text-muted-foreground"
          />
        )}
      </span>
    );

  // Opens the step picker for this line. The line stays open until the picker finishes: it can type the "/" after all,
  // return to the line typing nothing, or close the line.
  const openPicker = (
    opener: PickerOpener,
    [before, after]: [string, string],
  ) => {
    const selection = window.getSelection();
    const at = selection?.rangeCount
      ? selection.getRangeAt(0).cloneRange()
      : null;
    slashAt.current = at;
    opener((keepLine, typeSlash = keepLine) => {
      if (slashAt.current !== at) return;
      slashAt.current = null;
      const el = ref.current;
      if (!keepLine) {
        setEditing(false);
        setOverLimit(false);
        onDone?.(el?.innerText ?? value);
        return;
      }
      if (el?.isConnected && at) {
        // Type the "/" into the line, so the DOM, the caret and onChange agree.
        el.focus();
        window.getSelection()?.removeAllRanges();
        window.getSelection()?.addRange(at);
        if (typeSlash) document.execCommand("insertText", false, "/");
        return;
      }
      if (typeSlash) onChange(`${before}/${after}`);
    });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLSpanElement>) => {
    // Keys that confirm an IME candidate belong to the IME.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    const mod = event.metaKey || event.ctrlKey;
    const split = ref.current ? splitAtCaret(ref.current) : null;
    // A menu opens only where a word or line starts, so "bob@example.com" and "Open /settings" stay text.
    const atWordStart = !split || /(^|\s)$/.test(split[0]);
    if (mod && event.key.toLowerCase() === "z") {
      event.preventDefault();
      runHistory(event.shiftKey);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      ref.current?.blur();
      return;
    }
    if (event.key === "Enter" && mod) {
      event.preventDefault();
      if (event.shiftKey) window.dispatchEvent(new Event(DOC_RUN_AGENT_EVENT));
      else onRunStep?.();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && (onEnter || !multiline)) {
      event.preventDefault();
      onEnter?.(split ? { before: split[0], after: split[1] } : null);
      return;
    }
    if (mod && event.key === "." && onSettings) {
      event.preventDefault();
      onSettings();
      return;
    }
    if (
      (event.key === "ArrowUp" || event.key === "ArrowDown") &&
      ((mod && !event.shiftKey && !event.altKey) ||
        (event.altKey && event.shiftKey && !mod)) &&
      onMove
    ) {
      if (onMove(event.key === "ArrowUp" ? -1 : 1)) event.preventDefault();
      return;
    }
    if (
      (event.key === "ArrowUp" || event.key === "ArrowDown") &&
      !mod &&
      !event.shiftKey &&
      !event.altKey &&
      ref.current &&
      caretOnEdge(ref.current, event.key === "ArrowUp" ? "first" : "last")
    ) {
      const lines = Array.from(
        ref.current
          .closest(IN_DOC)
          ?.querySelectorAll<HTMLElement>("[data-doc-line]") ?? [],
      );
      const next =
        lines[lines.indexOf(ref.current) + (event.key === "ArrowUp" ? -1 : 1)];
      if (next) {
        event.preventDefault();
        if (event.key === "ArrowDown" && !next.isContentEditable)
          armNextCaretAtStart();
        next.focus();
        // A line already open keeps its caret where it was; put it at the edge the arrow came from.
        if (next.isContentEditable) {
          const range = document.createRange();
          range.selectNodeContents(next);
          range.collapse(event.key === "ArrowDown");
          window.getSelection()?.removeAllRanges();
          window.getSelection()?.addRange(range);
        }
      }
      return;
    }
    if (event.key === "@" && references && ref.current && atWordStart) {
      event.preventDefault();
      const selection = window.getSelection();
      if (selection?.rangeCount) {
        caret.current = selection.getRangeAt(0).cloneRange();
      } else {
        caret.current = document.createRange();
        caret.current.selectNodeContents(ref.current);
        caret.current.collapse(false);
      }
      setReferenceQuery("");
      setReferenceGroups(references());
      return;
    }
    if (event.key === "Tab" && onTab && !mod && !event.altKey) {
      const handled = onTab(event.shiftKey);
      if (handled) event.preventDefault();
      if (typeof handled === "function")
        openPicker(handled, split ?? [ref.current?.innerText ?? "", ""]);
      return;
    }
    if (event.key === "Backspace" && onBackspaceAtStart && !mod) {
      if (
        window.getSelection()?.isCollapsed &&
        split?.[0] === "" &&
        onBackspaceAtStart()
      )
        event.preventDefault();
      return;
    }
    if (event.key === "/" && onSlash && !mod) {
      const sides: [string, string] = split ?? [
        ref.current?.innerText ?? "",
        "",
      ];
      const lineSoFar = sides[0].slice(sides[0].lastIndexOf("\n") + 1);
      if (!lineSoFar.trim()) {
        event.preventDefault();
        openPicker(onSlash, sides);
      }
    }
  };
  return (
    <Popover
      key="edit"
      open={referenceGroups !== null}
      // Leaving the menu types nothing: the "@" only opened it.
      onOpenChange={(open) => !open && closeReferences("")}
    >
      <PopoverAnchor asChild ref={anchorRef}>
        <span
          ref={ref}
          role="textbox"
          aria-label={label}
          data-doc-line
          aria-multiline={multiline}
          contentEditable="plaintext-only"
          suppressContentEditableWarning
          data-placeholder={placeholder}
          onInput={() => {
            const el = ref.current;
            let next = el?.innerText ?? "";
            const tooLong = maxLength !== undefined && next.length > maxLength;
            setOverLimit(tooLong);
            if (el && tooLong) {
              // Keep the text savable: cut it at the limit and say so under the line.
              next = next.slice(0, maxLength);
              el.innerText = next;
              const range = document.createRange();
              range.selectNodeContents(el);
              range.collapse(false);
              window.getSelection()?.removeAllRanges();
              window.getSelection()?.addRange(range);
            }
            emitted.current = next;
            onChange(next);
          }}
          onKeyDown={onKeyDown}
          onBlur={() => {
            if (caret.current || slashAt.current) return;
            setEditing(false);
            setOverLimit(false);
            onDone?.(ref.current?.innerText ?? value);
          }}
          className={cn(
            "block min-h-[1.55em] whitespace-pre-wrap rounded outline-none [overflow-wrap:anywhere] empty:before:font-normal empty:before:text-muted-foreground empty:before:content-[attr(data-placeholder)]",
            className,
          )}
        />
      </PopoverAnchor>
      {/* At the limit too: a pending note remounts as the real one on its first input, which resets overLimit. */}
      {(overLimit ||
        (maxLength !== undefined && value.length >= maxLength)) && (
        <span role="status" className="block text-xs text-destructive">
          A note holds up to {maxLength?.toLocaleString("en-US")} characters.
        </span>
      )}
      <PopoverContent
        align="start"
        collisionPadding={padding()}
        sticky="always"
        className={cn("w-[min(320px,calc(100vw-32px))] p-0", docMenuClass)}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <Command loop className="min-h-0">
          <CommandInput
            autoFocus
            placeholder="Insert a reference…"
            value={referenceQuery}
            onValueChange={setReferenceQuery}
          />
          <CommandList className="max-h-[min(300px,45vh)]">
            <CommandEmpty>
              {createInput
                ? "Nothing to reference yet. Type a name to create an input."
                : "Nothing to reference here."}
            </CommandEmpty>
            {(referenceGroups ?? []).map((group) => (
              <CommandGroup key={group.name} heading={group.name}>
                {group.items.map((item) => (
                  <CommandItem
                    key={item.key}
                    value={`${item.key} ${group.name}`}
                    onSelect={() => closeReferences(`{{ ${item.key} }}`)}
                    className="flex items-baseline gap-2"
                  >
                    <span className="rounded bg-muted px-[5px] text-[13px] font-medium">
                      {item.key}
                    </span>
                    <span className="min-w-0 truncate text-xs text-muted-foreground">
                      {item.hint}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
            {newInputKey && (
              <CommandGroup heading="New">
                <CommandItem
                  value={`create input ${newInputKey}`}
                  onSelect={() => {
                    if (createInput?.(newInputKey))
                      closeReferences(`{{ ${newInputKey} }}`);
                  }}
                  className="flex flex-col items-start gap-0"
                >
                  <span className="text-[13px]">
                    Create input "{newInputKey}"
                  </span>
                  <span className="text-xs text-muted-foreground">
                    Adds it to the inputs at the top
                  </span>
                </CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/** "saves invoices_output": click the name to rename the step. */
export function DocOutputName({ id, label }: { id: string; label: string }) {
  const { edit } = useContext(WorkflowDocActionsContext);
  const [draft, setDraft] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const pill =
    "inline-block max-w-full rounded bg-muted px-[5px] text-left align-baseline font-medium leading-[1.25] [overflow-wrap:anywhere]";
  if (!edit)
    return (
      <span className={pill} title={`${label}_output`}>
        {label}_output
      </span>
    );
  if (draft === null)
    return (
      <button
        type="button"
        title={`Rename ${label}_output`}
        onClick={() => setDraft(label)}
        className={cn(
          pill,
          "outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        {label}_output
      </button>
    );
  const commit = () => {
    const next = draft.trim();
    const error = next === label ? null : edit.rename(id, next);
    if (error) return setProblem(error);
    setDraft(null);
    setProblem(null);
  };
  return (
    <span className="inline-flex flex-col">
      <span className="inline-flex items-center">
        <input
          autoFocus
          aria-label="Step name"
          aria-invalid={Boolean(problem)}
          value={draft}
          size={Math.max(draft.length, 4)}
          onChange={(e) => {
            setDraft(e.target.value);
            setProblem(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            }
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setDraft(null);
              setProblem(null);
            }
          }}
          onBlur={() => {
            if (draft.trim() === label || checkOk(draft)) commit();
          }}
          className={cn(
            "rounded border border-input bg-background px-1 font-medium leading-[1.25] text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring",
            problem && "border-destructive",
          )}
        />
        <span className="font-medium">_output</span>
      </span>
      {problem && (
        <span role="alert" className="text-xs text-destructive">
          {problem}
        </span>
      )}
    </span>
  );
}

const checkOk = (draft: string) =>
  /^[A-Za-z_][A-Za-z0-9_]*$/.test(draft.trim());

/** Asks before deleting a step (or a branch, given `remove` and `users`) whose output other steps use. */
export function DocDeleteConfirm({
  id,
  onClose,
  children,
  title = "Delete this step?",
  confirmLabel = "Delete",
  users: givenUsers,
  remove,
}: {
  id: string | null;
  onClose: () => void;
  children: ReactNode;
  title?: string;
  confirmLabel?: string;
  users?: Array<{ id: string; label: string; text: string }>;
  remove?: () => void;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const users = givenUsers ?? (id ? actions.outputUsers(id) : []);
  return (
    <Popover open={Boolean(id)} onOpenChange={(open) => !open && onClose()}>
      <PopoverAnchor asChild>{children}</PopoverAnchor>
      <PopoverContent
        align="start"
        role="alertdialog"
        aria-label={title}
        className="w-[min(320px,calc(100vw-32px))] p-3"
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          (e.currentTarget as HTMLElement)
            .querySelector<HTMLButtonElement>("[data-cancel]")
            ?.focus();
        }}
      >
        <p className="text-sm font-semibold">{title}</p>
        <p className="mt-1 text-[13px] text-muted-foreground">
          {users.length === 1 ? "This step uses" : "These steps use"}{" "}
          {remove ? "outputs of steps in this branch:" : "its output:"}
        </p>
        <ul className="mt-1.5 flex flex-col gap-0.5">
          {users.map((user) => (
            <li key={user.id}>
              <button
                type="button"
                onClick={() => actions.goTo(user.id)}
                className="flex w-full min-w-0 items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[13px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="shrink-0 font-semibold">{user.label}</span>
                <span className="truncate text-muted-foreground">
                  {user.text}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex justify-end gap-2">
          <Button data-cancel size="sm" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            variant="destructive"
            onClick={() => {
              if (remove) remove();
              else if (id) actions.edit?.remove(id);
              onClose();
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Keyboard-reachable list of places a step can move to. */
export function DocMoveToDialog({
  open,
  onOpenChange,
  destinations,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  destinations: DocDestination[];
  onPick: (destination: DocDestination) => void;
}) {
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Move to…" />
      <CommandList>
        <CommandEmpty>No place matches.</CommandEmpty>
        <CommandGroup heading="Move to the start of">
          {destinations.map((d, i) => (
            <CommandItem
              key={`${d.scope.parentId}:${d.scope.conditionalBranchId}:${i}`}
              value={`${d.label} ${i}`}
              onSelect={() => {
                onOpenChange(false);
                onPick(d);
              }}
            >
              {d.label}
            </CommandItem>
          ))}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}

/** Unsaved marker, undo/redo and Save. Each shows only when it applies. */
// `floating` (sm and up) lays the bar over the page's top padding (the parent positions it), so it adds no height
// and the Inputs row sits where v26 puts it, while the doc still doesn't jump on the first edit or on Save.
// While a Copilot proposal is pending its Accept / Reject bar is the one decision, so this bar steps aside.
export function DocEditBar({
  floating = false,
  proposalPending = false,
}: {
  floating?: boolean;
  proposalPending?: boolean;
}) {
  const hasChanges = useWorkflowHasChangesStore((s) => s.hasChanges);
  const canUndo = useWorkflowHistoryAccessStore((s) => s.canUndo);
  const canRedo = useWorkflowHistoryAccessStore((s) => s.canRedo);
  const { undoShortcutLabel, redoShortcutLabel } = useUndoRedoShortcutLabels();
  const save = useSaveWorkflow();
  const [saving, setSaving] = useState(false);
  const [saveFailure, setSaveFailure] = useState<string | null>(null);
  // The studio's own dirty check: the draft is back to the saved baseline after a user edit.
  const matchesSaved = useWorkflowSnapshotStore(
    (s) => s.snapshot !== null && s.userHasEdited && !s.contentDirty,
  );
  const pendingGoalChangeCount = useCopilotActionStore(
    (state) => state.pendingGoalChanges.length,
  );
  const [goalDialogOpen, setGoalDialogOpen] = useState(false);
  const saveNow = () => {
    // Same check as the studio Save: a new Goal whose code isn't regenerated is applied or undone first.
    if (pendingGoalChangeCount > 0) {
      setGoalDialogOpen(true);
      return;
    }
    runSave();
  };
  const saveShortcut = useRef<() => void>(() => {});
  saveShortcut.current = () => {
    if (!proposalPending && hasChanges && !saving) saveNow();
  };
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (
        event.isComposing ||
        event.altKey ||
        !(event.metaKey || event.ctrlKey) ||
        event.key.toLowerCase() !== "s"
      )
        return;
      // The browser's own "save page" has no meaning in the Doc.
      event.preventDefault();
      saveShortcut.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  const runSave = () => {
    setSaving(true);
    setSaveFailure(null);
    // The save that fails is the one started here, even if the editor has moved on to another workflow by then.
    const owner = useWorkflowYamlEditorStore.getState().editorOwner;
    void save()
      .catch((error: unknown) => {
        releaseUnansweredSave(error, owner);
        setSaveFailure(describeSaveFailure(error));
      })
      .finally(() => setSaving(false));
  };
  useEffect(() => {
    if (hasChanges && matchesSaved && !saving) {
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    }
  }, [hasChanges, matchesSaved, saving]);
  useEffect(() => {
    if (!hasChanges) setSaveFailure(null);
  }, [hasChanges]);
  if (proposalPending || (!hasChanges && !canUndo && !canRedo))
    return floating ? (
      <div aria-hidden className="mb-3 h-[28px] sm:hidden" />
    ) : null;
  return (
    <div
      className={cn(
        "mb-3 flex min-h-[28px] flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-muted-foreground",
        floating && "sm:mb-0 sm:flex-nowrap",
      )}
    >
      {hasChanges && saveFailure && !saving ? (
        <span
          role="alert"
          className="min-w-0 truncate text-xs font-normal text-destructive"
          title={saveFailure}
        >
          Couldn't save: {saveFailure}
        </span>
      ) : hasChanges ? (
        <span className="min-w-0 truncate text-xs font-normal text-[#B45B0A] dark:text-[#E79A55]">
          Unsaved changes
        </span>
      ) : (
        <span>Saved</span>
      )}
      <span className="flex-1" />
      {canUndo && (
        <Button
          size="sm"
          variant="ghost"
          className="h-7 shrink-0 gap-1 px-2"
          title={`Undo (${undoShortcutLabel})`}
          onClick={() => runHistory(false)}
        >
          <ResetIcon className="size-3.5" />
          Undo
        </Button>
      )}
      {canRedo && (
        <Button
          size="sm"
          variant="ghost"
          className="h-7 shrink-0 gap-1 px-2"
          title={`Redo (${redoShortcutLabel})`}
          onClick={() => runHistory(true)}
        >
          <ResetIcon className="size-3.5 -scale-x-100" />
          Redo
        </Button>
      )}
      {hasChanges && (
        <Button
          size="sm"
          className="h-7 shrink-0 px-3"
          disabled={saving}
          onClick={saveNow}
        >
          {saving ? "Saving…" : saveFailure ? "Try again" : "Save"}
        </Button>
      )}
      <PendingGoalChangesDialog
        open={goalDialogOpen}
        onOpenChange={setGoalDialogOpen}
        onSave={runSave}
      />
    </div>
  );
}
