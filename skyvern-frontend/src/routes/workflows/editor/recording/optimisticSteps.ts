import {
  nextOptimisticStepId,
  type MessageInExfiltratedEvent,
  type OptimisticStep,
} from "@/store/useRecordingStore";

/**
 * Human label for an interaction target, mirroring the backend's
 * `_action_display_text` (interpretation.py) so optimistic titles match the
 * interpreted ones they are replaced by.
 */
function actionDisplayText(target: {
  text?: Array<string>;
  innerText?: string | null;
  tagName?: string | null;
}): string {
  for (const text of target.text ?? []) {
    const cleaned = text.split(/\s+/).filter(Boolean).join(" ");
    if (cleaned) {
      return cleaned.slice(0, 80);
    }
  }
  const inner = target.innerText?.split(/\s+/).filter(Boolean).join(" ");
  if (inner) {
    return inner.slice(0, 80);
  }
  return (target.tagName ?? "element").toLowerCase();
}

/**
 * Build an optimistic placeholder for a single exfiltrated event, or null when
 * the event does not map to a step. Only clicks and the end of an input
 * (change/blur with a value) produce placeholders — keystrokes, focus, and
 * navigations are elided (a link-click navigation is already represented by its
 * click, and the backend emits no goto for it).
 */
export function buildOptimisticStep(
  message: MessageInExfiltratedEvent,
): OptimisticStep | null {
  if (message.source !== "console") {
    return null;
  }

  const { params } = message;
  if (params.type === "click") {
    return {
      local_id: nextOptimisticStepId(),
      action_kind: "click",
      title: `Click '${actionDisplayText(params.target)}'`,
      timestamp: params.timestamp,
    };
  }

  if (params.type === "change" || params.type === "blur") {
    const value = params.inputValue ?? params.target.value ?? "";
    if (value.trim() === "") {
      return null;
    }
    return {
      local_id: nextOptimisticStepId(),
      action_kind: "input_text",
      title: `Fill '${actionDisplayText(params.target)}'`,
      timestamp: params.timestamp,
    };
  }

  return null;
}
