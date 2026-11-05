import type { Node } from "@xyflow/react";
import { getDocProseFields } from "./docStepRegistry";

// A Browser Action runs one action. An instruction that joins steps ("search, then read the price")
// or runs to a second sentence needs a Browser Task, which can take several actions.
const SEVERAL_ACTIONS = /\b(?:and|then|after that|afterwards)\b|[.;]\s+\S/i;

export function docOneActionStepWantsTask(node: Node): boolean {
  if (node.type !== "action") return false;
  const key = getDocProseFields(node)[0]?.key;
  const value = key ? (node.data as Record<string, unknown>)[key] : null;
  return (
    typeof value === "string" &&
    SEVERAL_ACTIONS.test(value.replace(/\{\{[\s\S]*?\}\}/g, ""))
  );
}
