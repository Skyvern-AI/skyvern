import { type StudioPaneId } from "./panes";

type RecordingPaneTransition = "started" | "processing" | "ended";

export function panesAfterRecordingTransition(
  panes: readonly StudioPaneId[],
  transition: RecordingPaneTransition,
): StudioPaneId[] {
  const next = panes.filter((pane) => {
    if (transition === "started") {
      return pane !== "editor";
    }
    if (transition === "processing") {
      return pane !== "browser";
    }
    return true;
  });
  const requiredPanes: StudioPaneId[] =
    transition === "started" ? ["copilot", "browser"] : ["editor"];

  for (const pane of requiredPanes) {
    if (!next.includes(pane)) {
      next.push(pane);
    }
  }

  return next;
}
