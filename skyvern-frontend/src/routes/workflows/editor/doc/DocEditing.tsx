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
import { caretOnEdge, placeCaretAt, splitAtCaret } from "./docCaret";
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
import { DocKeyLabel, DocKeyTooltip } from "./DocKeyLabel";
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
  docShortcut,
  rememberReferenceLine,
  takeNextCaretOffset,
  takeNextCaretAtStart,
  type LineSplit,
  type PickerOpener,
} from "./docKeys";
import { DocText } from "./DocText";
import { pastedFiles } from "./docImageUploads";
import { DocLongText, type DocTextCollapse } from "./DocLongText";
import { saveAndReport, useDocSaveFailureStore } from "./docSaveState";
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
  onDemonstrate,
  exclude = [],
  placeholder = "Add a step…",
}: {
  onPick: (nodeType: NodeType) => void;
  // Offers "Demonstrate task" (first while nothing is typed); left out where a demonstration can't start.
  onDemonstrate?: () => void;
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
  const query = search.trim();
  // cmdk highlights the first visible item, so a typed query lists this after the steps.
  // One or two letters would match only its keywords, so it waits for three.
  const demonstrate = onDemonstrate && (!query || query.length >= 3) && (
    <CommandGroup heading="Copilot">
      <CommandItem
        value="Demonstrate task"
        keywords={["demonstrate", "record", "teach"]}
        onSelect={onDemonstrate}
        className="gap-2.5"
      >
        <span className="grid size-7 shrink-0 place-items-center rounded-md border border-border">
          <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-red-500" />
        </span>
        <span className="min-w-0">
          <span className="block text-sm leading-tight">Demonstrate task</span>
          <span className="block text-xs leading-tight text-muted-foreground">
            Show Copilot how in the browser
          </span>
        </span>
      </CommandItem>
    </CommandGroup>
  );
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
        {!query && demonstrate}
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
        {query && demonstrate}
      </CommandList>
    </Command>
  );
}

/** A popover anchored to `children` that holds the step picker. */
export function DocInsertPopover({
  open,
  onOpenChange,
  onPick,
  onDemonstrate,
  onDismiss,
  onEscape,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (nodeType: NodeType) => void;
  onDemonstrate?: (() => void) | null;
  // Closed without picking a step.
  onDismiss?: () => void;
  // Escape closed it, so focus has nowhere to return to unless the caller puts it back.
  onEscape?: () => void;
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
        onEscapeKeyDown={onEscape}
      >
        <DocStepPicker
          onPick={(type) => {
            onOpenChange(false);
            onPick(type);
          }}
          onDemonstrate={
            onDemonstrate
              ? () => {
                  onOpenChange(false);
                  onDemonstrate();
                }
              : undefined
          }
        />
      </PopoverContent>
    </Popover>
  );
}

// A collapsed caret `offset` characters into a line's text, counting each <br> as one.
function caretRangeAt(el: HTMLElement, offset: number): Range {
  const range = document.createRange();
  let left = offset;
  const walker = document.createTreeWalker(
    el,
    NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
  );
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === Node.TEXT_NODE) {
      const length = node.nodeValue?.length ?? 0;
      if (left <= length) {
        range.setStart(node, left);
        range.collapse(true);
        return range;
      }
      left -= length;
    } else if (node.nodeName === "BR") {
      if (left === 0) {
        range.setStartBefore(node);
        range.collapse(true);
        return range;
      }
      left -= 1;
    }
  }
  range.selectNodeContents(el);
  range.collapse(false);
  return range;
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
  editingPlaceholder = placeholder,
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
  pickerOnStart,
  maxLength,
  collapse,
  onFiles,
  onMenuActiveChange,
}: {
  value: string;
  editable: boolean;
  placeholder?: string;
  // Shown in place of `placeholder` while the empty line has the caret.
  editingPlaceholder?: string;
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
  // Both return false when they did nothing, so the key keeps its usual meaning.
  onTab?: (outdent: boolean) => boolean;
  onBackspaceAtStart?: () => boolean;
  // Moves the step this line belongs to up (-1) or down (1).
  onMove?: (delta: -1 | 1) => boolean;
  onSettings?: () => void;
  onRunStep?: () => void;
  // True when entering the line should put the caret at its start.
  caretAtStart?: () => boolean;
  // True when entering the line should open the step picker on it, as "/" would.
  pickerOnStart?: () => boolean;
  maxLength?: number;
  // A step's main text folds to its first lines when it is long; notes and secondary lines leave this out.
  collapse?: DocTextCollapse;
  // Files pasted into the line; without it a paste is the browser's own.
  onFiles?: (files: File[]) => void;
  // Reports each menu close after any focus restoration, including lock or unmount.
  onMenuActiveChange?: (open: boolean) => void;
}) {
  const [editing, setEditing] = useState(autoFocus);
  const [overLimit, setOverLimit] = useState(false);
  const { anchorRef, padding } = useDocBoundary();
  const ref = useRef<HTMLSpanElement | null>(null);
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
  const menuActive = useRef(false);
  const reportMenuRef = useRef(onMenuActiveChange);
  reportMenuRef.current = onMenuActiveChange;
  const reportMenu = (open: boolean) => {
    if (menuActive.current === open) return;
    menuActive.current = open;
    reportMenuRef.current?.(open);
  };
  useEffect(() => {
    if (!editable && menuActive.current) {
      caret.current = null;
      slashAt.current = null;
      setReferenceGroups(null);
      reportMenu(false);
    }
  }, [editable]);
  useEffect(
    () => () => {
      if (menuActive.current) reportMenuRef.current?.(false);
    },
    [],
  );
  const closeReferences = (insert: string) => {
    // Refocusing the line counts as leaving the menu, which calls this again; only the first call inserts.
    const range = caret.current;
    if (!range) return;
    caret.current = null;
    setReferenceGroups(null);
    const el = ref.current;
    if (!el) {
      reportMenu(false);
      return;
    }
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
    reportMenu(false);
    rememberReferenceLine(
      label
        ? {
            label,
            index: same.indexOf(el),
            offset: splitAtCaret(el)?.[0].length ?? el.innerText.length,
          }
        : null,
    );
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
  // A save locks the Doc read-only while the line stays in edit mode; the edit span then remounts empty.
  const resumed = useRef(false);
  // Where the caret or selection was at the last key, so a Cmd+S puts it back there.
  const keyCaret = useRef<[number, number] | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!editing) return;
    if (!editable || !el) {
      resumed.current = true;
      return;
    }
    const resuming = resumed.current;
    resumed.current = false;
    el.innerText = value;
    // Back from read-only, the caret returns only if nothing else took focus meanwhile.
    const elsewhere =
      document.activeElement && document.activeElement !== document.body;
    if (resuming && elsewhere) return;
    el.focus();
    let range: Range;
    if (resuming) {
      const [start, end] = keyCaret.current ?? [value.length, value.length];
      range = caretRangeAt(el, start);
      const endAt = caretRangeAt(el, end);
      range.setEnd(endAt.endContainer, endAt.endOffset);
    } else {
      keyCaret.current = null;
      range = document.createRange();
      range.selectNodeContents(el);
      const atStart = caretAtStart?.() ?? false;
      range.collapse(takeNextCaretAtStart() || atStart);
    }
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    const offset = takeNextCaretOffset();
    if (offset !== null) placeCaretAt(el, offset);
    if (!resuming && onSlash && pickerOnStart?.())
      openPicker(onSlash, [value, ""]);
    // Only on entering edit mode, or coming back to it after a read-only spell.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, editable]);
  useEffect(() => {
    if (autoFocus) setEditing(true);
  }, [autoFocus]);
  // Every way into editing opens the text, including a line that starts in editing (a new step's autofocus).
  // A layout effect, so the editing line is never painted folded. `collapse` stays out of the dependencies on purpose:
  // a later open=false (Show less) must not re-open the text while the line is being edited.
  useLayoutEffect(() => {
    if (editing && editable) collapse?.setOpen(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, editable]);

  const shownText = value.trim() ? (
    <DocText value={value} />
  ) : (
    <DocText
      value={placeholder ?? ""}
      className="font-normal text-muted-foreground"
    />
  );
  // Distinct keys: the editing span holds browser-owned text React must not reuse. One wrapper key across read-only,
  // viewing and editing keeps Show less mounted, so it keeps focus when the line closes or the Doc locks.
  if (!editable)
    return (
      <DocLongText key="line" collapse={collapse} value={value}>
        {(shown) => (
          <span
            ref={shown.ref}
            id={shown.id}
            style={shown.style}
            className={cn(collapse && "block", className)}
          >
            {shownText}
          </span>
        )}
      </DocLongText>
    );
  if (!editing)
    return (
      <DocLongText key="line" collapse={collapse} value={value}>
        {(shown) => (
          <span
            key="view"
            ref={shown.ref}
            id={shown.id}
            style={shown.style}
            data-doc-line
            role="textbox"
            tabIndex={0}
            aria-label={label}
            onClick={() => setEditing(true)}
            onFocus={() => setEditing(true)}
            className={cn("block cursor-text rounded outline-none", className)}
          >
            {shownText}
          </span>
        )}
      </DocLongText>
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
    reportMenu(true);
    opener((keepLine, typeSlash = keepLine) => {
      if (slashAt.current !== at) return;
      slashAt.current = null;
      const el = ref.current;
      if (!keepLine) {
        setEditing(false);
        setOverLimit(false);
        onDone?.(el?.innerText ?? value);
        reportMenu(false);
        return;
      }
      if (el?.isConnected && at) {
        // Type the "/" into the line, so the DOM, the caret and onChange agree.
        el.focus();
        window.getSelection()?.removeAllRanges();
        window.getSelection()?.addRange(at);
        if (typeSlash) document.execCommand("insertText", false, "/");
        reportMenu(false);
        return;
      }
      if (typeSlash) onChange(`${before}/${after}`);
      reportMenu(false);
    });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLSpanElement>) => {
    // Keys that confirm an IME candidate belong to the IME.
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    const mod = event.metaKey || event.ctrlKey;
    const split = ref.current ? splitAtCaret(ref.current) : null;
    keyCaret.current =
      split && ref.current
        ? [
            split[0].length,
            Math.max(
              split[0].length,
              ref.current.innerText.length - split[1].length,
            ),
          ]
        : null;
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
      // Focus stays on the step: its type label, not the page.
      const tag = ref.current
        ?.closest("[data-doc-node-id]")
        ?.querySelector<HTMLElement>("[data-doc-tag]");
      ref.current?.blur();
      tag?.focus();
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
      reportMenu(true);
      setReferenceQuery("");
      setReferenceGroups(references());
      return;
    }
    if (event.key === "Tab" && !mod && !event.altKey) {
      // Only a Tab that nests (or a Shift+Tab that outdents) is taken; otherwise focus moves on, so Tab is no keyboard trap.
      if (onTab?.(event.shiftKey)) event.preventDefault();
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
    <DocLongText key="line" collapse={collapse} value={value}>
      {(shown) => {
        // The editing span is not measured: its raw text wraps unlike the view's reference pills, so the control
        // keeps what the view measured last.
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
                id={shown.id}
                role="textbox"
                aria-label={label}
                data-doc-line
                aria-multiline={multiline}
                contentEditable="plaintext-only"
                suppressContentEditableWarning
                data-placeholder={editingPlaceholder}
                aria-placeholder={editingPlaceholder}
                onInput={() => {
                  const el = ref.current;
                  let next = el?.innerText ?? "";
                  const tooLong =
                    maxLength !== undefined && next.length > maxLength;
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
                onPaste={(event) => {
                  const files = onFiles ? pastedFiles(event.clipboardData) : [];
                  if (!files.length) return;
                  event.preventDefault();
                  onFiles?.(files);
                }}
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
                A note holds up to {maxLength?.toLocaleString("en-US")}{" "}
                characters.
              </span>
            )}
            <PopoverContent
              align="start"
              collisionPadding={padding()}
              sticky="always"
              className={cn(
                "w-[min(320px,calc(100vw-32px))] p-0",
                docMenuClass,
              )}
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
                            {item.label ?? item.key}
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
      }}
    </DocLongText>
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
  onRemoved,
}: {
  id: string | null;
  onClose: () => void;
  children: ReactNode;
  title?: string;
  confirmLabel?: string;
  users?: Array<{ id: string; label: string; text: string }>;
  remove?: () => void;
  // After the step itself is deleted, not after `remove`.
  onRemoved?: () => void;
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
              else if (id && actions.edit?.remove(id)) onRemoved?.();
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
  const workflowPermanentId = useWorkflowYamlEditorStore(
    (s) => s.editorOwner?.workflowPermanentId,
  );
  const saveFailure = useDocSaveFailureStore((s) =>
    workflowPermanentId ? s.failures[workflowPermanentId] : undefined,
  );
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
    void saveAndReport(save).finally(() => setSaving(false));
  };
  useEffect(() => {
    if (hasChanges && matchesSaved && !saving) {
      useWorkflowHasChangesStore.getState().setHasChanges(false);
    }
  }, [hasChanges, matchesSaved, saving]);
  useEffect(() => {
    if (!hasChanges && workflowPermanentId)
      useDocSaveFailureStore.getState().setFailure(workflowPermanentId, null);
  }, [hasChanges, workflowPermanentId]);
  // A save made while the Doc is hidden (Visual, Code) never clears the entry, so the bar drops it when it unmounts.
  useEffect(
    () => () => {
      if (workflowPermanentId)
        useDocSaveFailureStore.getState().setFailure(workflowPermanentId, null);
    },
    [workflowPermanentId],
  );
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
        <DocKeyTooltip
          content={
            <>
              <DocKeyLabel label="Save" keys="S" />
            </>
          }
        >
          <Button
            size="sm"
            className="h-7 shrink-0 px-3"
            disabled={saving}
            aria-keyshortcuts={docShortcut("S").aria}
            onClick={saveNow}
          >
            {saving ? "Saving…" : saveFailure ? "Try again" : "Save"}
          </Button>
        </DocKeyTooltip>
      )}
      <PendingGoalChangesDialog
        open={goalDialogOpen}
        onOpenChange={setGoalDialogOpen}
        onSave={runSave}
      />
    </div>
  );
}
