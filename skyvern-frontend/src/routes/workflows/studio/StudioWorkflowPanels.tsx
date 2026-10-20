import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { WorkflowParametersPanel } from "../editor/panels/WorkflowParametersPanel";
import { WorkflowSchedulePanel } from "../editor/panels/schedulePanel/WorkflowSchedulePanel";
import { usePageSlots } from "@/store/PageSlots";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

/**
 * Inputs / Schedule / Triggers panels render at shell level so they open over any tab —
 * the editor canvas is display:none on Browser/Overview tabs and would hide them.
 */
export function StudioWorkflowPanels() {
  const state = useWorkflowPanelStore((s) => s.workflowPanelState);
  const close = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const { workflowTriggersPanel: WorkflowTriggersPanel } = usePageSlots();
  const workflowPermanentId = useWorkflowPermanentId();

  const content =
    state.active &&
    (state.content === "parameters" ||
      state.content === "schedules" ||
      state.content === "triggers")
      ? state.content
      : null;
  if (!content) {
    return null;
  }
  if (content === "triggers" && !WorkflowTriggersPanel) return null;

  return (
    <>
      <div className="absolute inset-0 z-30" onClick={close} />
      {content === "parameters" ? (
        // No overflow clipping: the "Add Input" form opens to the panel's left
        // (negative offset), and overflow-y:auto would force overflow-x and clip it.
        <div className="absolute right-3 top-3 z-40">
          <WorkflowParametersPanel />
        </div>
      ) : content === "schedules" ? (
        <div className="absolute right-3 top-3 z-40 max-h-[calc(100%-1.5rem)] overflow-y-auto">
          <WorkflowSchedulePanel onClose={close} />
        </div>
      ) : (
        <div className="absolute right-3 top-3 z-40 max-h-[calc(100%-1.5rem)] overflow-y-auto">
          {WorkflowTriggersPanel ? (
            <WorkflowTriggersPanel
              workflowPermanentId={workflowPermanentId ?? ""}
              onClose={close}
            />
          ) : null}
        </div>
      )}
    </>
  );
}
