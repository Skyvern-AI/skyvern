import type { Node } from "@xyflow/react";
import type { DocPromptUrlOffer } from "./docPromptUrl";
import { getDocProseFields } from "./docStepRegistry";

// A Browser Action runs one action. An instruction that joins steps ("search, then read the price")
// or runs to a second sentence needs a Browser Task, which can take several actions. A bare "and"
// does not count: "enter username and password" joins two objects of one action, so "and" only
// counts when a new action verb follows it.
const ACTION_VERBS =
  "click|open|search|go|get|read|type|enter|select|fill|press|scroll|navigate|find|extract|submit|log|sign|download|upload|copy|check|add|wait|take|close|visit|choose|save|write|return|tap|hover|drag|paste|pick|toggle|expand|accept|confirm|login|refresh|reload|view|show|collapse|delete|remove|edit|update|create|send|post|play|pause|start|stop|continue|switch|change|set|apply|clear|cancel|dismiss|skip|reply|sort|filter|print|export|import|verify|press|look|navigate|wait";
const SEVERAL_ACTIONS = new RegExp(
  `\\b(?:then|after that|afterwards)\\b|\\band\\s+(?:${ACTION_VERBS})\\b|[.;]\\s+\\S`,
  "i",
);

export function docOneActionStepWantsTask(node: Node): boolean {
  if (node.type !== "action") return false;
  const key = getDocProseFields(node)[0]?.key;
  const value = key ? (node.data as Record<string, unknown>)[key] : null;
  return (
    typeof value === "string" &&
    SEVERAL_ACTIONS.test(value.replace(/\{\{[\s\S]*?\}\}/g, ""))
  );
}

// Dismissals survive branch switches that unmount a step.
export const dismissedDocSuggestions = new Set<string>();

export function isDocUrlOfferVisible(
  node: Node,
  offer: DocPromptUrlOffer | null,
) {
  return Boolean(
    offer &&
    !dismissedDocSuggestions.has(`url:${node.id}:${offer.url}`) &&
    (dismissedDocSuggestions.has(`one-action:${node.id}`) ||
      !docOneActionStepWantsTask(node)),
  );
}
