import type { AppNode } from "../nodes";
import { WORKFLOW_WAIT_BLOCK_MAX_SEC } from "../workflowEditorUtils";
import { loopSourceKey } from "./docStepRegistry";

const FIRST_REFERENCE = /\{\{[^{}]*\}\}/;
function lineList(line: string): string {
  return line.match(FIRST_REFERENCE)?.[0] ?? "";
}

const WAIT_AMOUNT =
  /(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)?\b/i;

/** "wait 10 seconds", "2 min", "1.5 hours" as whole seconds within the Wait's bounds; null when the line has no amount. */
export function waitSecondsFromLine(line: string): number | null {
  const match = line.match(WAIT_AMOUNT);
  if (!match) return null;
  const unit = (match[2] ?? "s").toLowerCase();
  const factor = unit.startsWith("h") ? 3600 : unit.startsWith("m") ? 60 : 1;
  const seconds = Math.round(Number(match[1]) * factor);
  return Math.min(Math.max(seconds, 1), WORKFLOW_WAIT_BLOCK_MAX_SEC);
}

/**
 * The setting a for-each loop's Doc line also writes: the list it goes through.
 */
export function withLineSetting(
  node: AppNode,
  key: string,
  previousLine: string,
): AppNode {
  if (key !== "description") return node;
  const data = node.data as Record<string, unknown>;
  const line = typeof data.description === "string" ? data.description : "";
  if (node.type === "wait") {
    const seconds = waitSecondsFromLine(line);
    if (seconds === null || line === previousLine) return node;
    return {
      ...node,
      data: { ...node.data, waitInSeconds: String(seconds) },
    } as AppNode;
  }
  if (node.type === "loop" && data.loopKind !== "while") {
    // The line is free text; only a list reference typed or changed on it becomes the source, in the field that holds it.
    const reference = lineList(line);
    const previous = lineList(previousLine);
    if (!reference) {
      // Taking the reference off the line takes the list away; leaving it saved would loop over a list the line no longer names.
      if (!previous) return node;
      return {
        ...node,
        data: { ...node.data, loopValue: "", loopVariableReference: "" },
      } as AppNode;
    }
    if (reference === previous) return node;
    // loopValue is saved verbatim as loop_over_parameter_key, an exact input name, so it takes the bare key.
    const key = loopSourceKey(node);
    const bare = reference.slice(2, -2).trim();
    const patch =
      key === "loopValue"
        ? /^[A-Za-z_][A-Za-z0-9_]*$/.test(bare)
          ? { loopValue: bare }
          : { loopVariableReference: reference }
        : { [key]: reference };
    return { ...node, data: { ...node.data, ...patch } } as AppNode;
  }
  return node;
}
