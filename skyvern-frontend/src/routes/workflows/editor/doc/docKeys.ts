import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import { isMacPlatform } from "@/util/platform";

/** Fired by the run-agent shortcut; the top-bar Run button opens its "Run with" box (or runs, if it is open). */
export const DOC_RUN_AGENT_EVENT = "skyvern:doc-run-agent";

/** What the caret split a line into. */
export type LineSplit = { before: string; after: string };

/** The step picker, opened by a key other than "/". `finish(true, false)` returns to the line and types nothing. */
export type PickerFinish = (keepLine: boolean, typeSlash?: boolean) => void;
export type PickerOpener = (finish: PickerFinish) => void;

export const modLabel = () => (isMacPlatform() ? "⌘" : "Ctrl+");

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
