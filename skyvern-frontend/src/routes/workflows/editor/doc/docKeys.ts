import { useSyncExternalStore } from "react";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import { isMacPlatform } from "@/util/platform";

/** Fired by the run-agent shortcut; the top-bar Run button handles it as a click. */
export const DOC_RUN_AGENT_EVENT = "skyvern:doc-run-agent";
/** Fired by "Fix problems" in the Run confirmation; the Doc opens its problems list. */
export const DOC_SHOW_PROBLEMS_EVENT = "skyvern:doc-show-problems";
/**
 * Where focus goes back to when Run's problems question or the Doc's problems list closes. A line's edit span is
 * replaced when it loses focus, so the element can be gone; the line is then found again in its step by aria-label
 * and its index among lines with that label (every Note is labelled "Note").
 */
export type DocReturnPoint = {
  element: HTMLElement;
  stepId: string | null;
  lineLabel: string | null;
  lineIndex: number;
};
/** Fired by "Fix problems" in the Run confirmation; null when focus was nowhere to return to. */
export type DocShowProblemsDetail = { returnPoint: DocReturnPoint | null };

function docStep(root: ParentNode, stepId: string): HTMLElement | undefined {
  return Array.from(
    root.querySelectorAll<HTMLElement>("[data-doc-node-id]"),
  ).find((el) => el.dataset.docNodeId === stepId);
}

function linesLabelled(step: ParentNode, label: string): HTMLElement[] {
  return Array.from(
    step.querySelectorAll<HTMLElement>("[data-doc-line]"),
  ).filter((line) => line.getAttribute("aria-label") === label);
}

export function captureReturnPoint(el: Element | null): DocReturnPoint | null {
  if (!(el instanceof HTMLElement)) return null;
  const step = el.closest<HTMLElement>("[data-doc-node-id]");
  const line = el.closest<HTMLElement>("[data-doc-line]");
  const lineLabel = line?.getAttribute("aria-label") ?? null;
  return {
    element: el,
    stepId: step?.dataset.docNodeId ?? null,
    lineLabel,
    lineIndex:
      step && line && lineLabel !== null
        ? Math.max(0, linesLabelled(step, lineLabel).indexOf(line))
        : 0,
  };
}

export function resolveReturnPoint(
  root: ParentNode | null,
  point: DocReturnPoint | null,
): HTMLElement | null {
  if (!point) return null;
  if (point.element.isConnected) return point.element;
  const step = root && point.stepId ? docStep(root, point.stepId) : undefined;
  if (!step) return null;
  const labelled =
    point.lineLabel === null ? [] : linesLabelled(step, point.lineLabel);
  return (
    labelled[point.lineIndex] ??
    labelled[0] ??
    step.querySelector<HTMLElement>("[data-doc-line]") ??
    null
  );
}

// A line's edit span is replaced when it loses focus, so a saved element can be gone: look up the step's first line.
export function docStepLine(
  root: ParentNode | null,
  stepId: string | null,
): HTMLElement | null {
  if (!root || !stepId) return null;
  return (
    docStep(root, stepId)?.querySelector<HTMLElement>("[data-doc-line]") ?? null
  );
}

/** What the caret split a line into. */
export type LineSplit = { before: string; after: string };

/** The step picker, opened by a key other than "/". `finish(true, false)` returns to the line and types nothing. */
export type PickerFinish = (keepLine: boolean, typeSlash?: boolean) => void;
export type PickerOpener = (finish: PickerFinish) => void;

export const modLabel = () => (isMacPlatform() ? "⌘" : "Ctrl+");

/** A Cmd/Ctrl shortcut as Undo's "⌘Z" is written, and as `aria-keyshortcuts` reads it. */
export function docShortcut(key: string, shift = false) {
  const mac = isMacPlatform();
  return {
    label: mac
      ? `⌘${shift ? "⇧" : ""}${key}`
      : `Ctrl+${shift ? "Shift+" : ""}${key}`,
    aria: `${mac ? "Meta" : "Control"}+${shift ? "Shift+" : ""}${key}`,
  };
}

/** Runs one step the way its Run button does; false when it cannot run now. */
export function runStepByKey(nodeId: string): boolean {
  const handle = useBlockPlayRegistry.getState().handles[nodeId];
  if (!handle || handle.inert || handle.playing) return false;
  handle.play();
  return true;
}

// The line an "@" reference was inserted in, so closing the input editor that opens can give it the caret back.
// Notes all share one label, so the line is told apart by its place among the lines with that label.
type ReferenceLine = { label: string; index: number; offset: number };
let referenceLine: ReferenceLine | null = null;
export const rememberReferenceLine = (line: ReferenceLine | null) => {
  referenceLine = line;
};
export const takeReferenceLine = (): ReferenceLine | null => {
  const label = referenceLine;
  referenceLine = null;
  return label;
};

// The insert line between steps opens an empty line with the step picker already open on it.
let pickerOnLine: string | null = null;
export const requestPickerOnLine = (lineKey: string) => {
  pickerOnLine = lineKey;
};
export const takePickerOnLine = (lineKey: string): boolean => {
  if (pickerOnLine !== lineKey) return false;
  pickerOnLine = null;
  return true;
};

// A step made by splitting a line puts the caret at the start of its text, not the end.
let caretAtStartFor: string | null = null;
export const requestCaretAtStart = (nodeId: string) => {
  caretAtStartFor = nodeId;
};
// Arrow keys into a closed line arm this so the line opens with the caret at its start.
let nextAtStart = false;
export const armNextCaretAtStart = () => {
  nextAtStart = true;
};
export const takeNextCaretAtStart = (): boolean => {
  const armed = nextAtStart;
  nextAtStart = false;
  return armed;
};
export const takeCaretAtStart = (nodeId: string): boolean => {
  if (caretAtStartFor !== nodeId) return false;
  caretAtStartFor = null;
  return true;
};

// Closing the input editor reopens the line with the caret where the reference ended.
let nextCaretOffset: number | null = null;
export const armNextCaretOffset = (offset: number) => {
  nextCaretOffset = offset;
};
export const takeNextCaretOffset = (): number | null => {
  const offset = nextCaretOffset;
  nextCaretOffset = null;
  return offset;
};

// Touch screens get no hover-only controls. Gated on the primary input device, not the width: a narrow window with
// a mouse keeps them; a touch laptop drops them only when touch is its primary pointer (tablet mode).
export const DOC_TOUCH_QUERY = "(hover: none), (pointer: coarse)";

function subscribeTouch(onChange: () => void) {
  if (typeof window.matchMedia !== "function") return () => {};
  const query = window.matchMedia(DOC_TOUCH_QUERY);
  query.addEventListener?.("change", onChange);
  return () => query.removeEventListener?.("change", onChange);
}

function isDocTouch(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    Boolean(window.matchMedia(DOC_TOUCH_QUERY)?.matches)
  );
}

export function useDocTouch(): boolean {
  return useSyncExternalStore(subscribeTouch, isDocTouch, () => false);
}
