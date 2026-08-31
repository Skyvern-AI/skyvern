import { InputIcon, PlusIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { cn } from "@/util/utils";

// A long input list would grow the start node without bound, and the canvas
// card is a summary, not the editor — the panel lists them all.
const MAX_VISIBLE_KEYS = 8;

/**
 * Count chip for the editor headers' Inputs control. `Badge` is px-2.5 py-1,
 * too tall to sit inside an h-8 toggle. aria-hidden: the toggle carries the
 * count in its own aria-label.
 */
function InputsCountBadge({
  count,
  className,
}: {
  count: number;
  className?: string;
}) {
  if (count === 0) {
    return null;
  }
  return (
    <span
      aria-hidden
      className={cn(
        "rounded-full bg-primary/15 px-1.5 text-[0.6875rem] font-semibold tabular-nums leading-4 text-foreground",
        className,
      )}
    >
      {count}
    </span>
  );
}

/**
 * Inputs, stated where the flow starts, because a header toggle was the only
 * door and nobody found it (SKY-14866). Its own card rather than a row in the
 * start node's settings accordion, which already carries thirteen configs.
 *
 * A summary, not an editor: the keys are text, and every action routes to the
 * existing Inputs panel.
 *
 * `editable` is the start node's own flag (false for global workflows and
 * deleted snapshots, matching what the editor headers hide). The summary still
 * renders when it is false — reading a view-only agent's inputs is fine — but
 * Add is gone, because WorkflowParametersPanel can mutate and would dirty a
 * workflow that cannot be saved.
 */
function WorkflowInputsCard({ editable }: { editable: boolean }) {
  const parameters = useWorkflowParametersStore((s) => s.parameters);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );
  const hidden = parameters.length - MAX_VISIBLE_KEYS;

  // Nothing to summarise and nothing to offer.
  if (!editable && parameters.length === 0) {
    return null;
  }

  return (
    // The card sits inside the start node, whose canvas click selects it and
    // expands Workflow Settings (FlowRenderer's onNodeClick). This summary is
    // its own surface, so no click in it reaches that.
    <div
      className="nodrag nopan w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-sm">
          <InputIcon className="size-4 text-muted-foreground" aria-hidden />
          Inputs
        </span>
        {editable ? (
          <Button
            variant="tertiary"
            size="sm"
            disabled={isRecording}
            onClick={() =>
              setWorkflowPanelState({ active: true, content: "parameters" })
            }
          >
            <PlusIcon className="mr-1 size-3.5" aria-hidden />
            Add
          </Button>
        ) : null}
      </div>
      {parameters.length === 0 ? (
        <p className="mt-2 text-left text-xs text-muted-foreground">
          None yet. Inputs are placeholder values you can link in any block, and
          fill in before each run.
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          {parameters.slice(0, MAX_VISIBLE_KEYS).map((parameter) => (
            <span
              key={parameter.key}
              title={parameter.key}
              className="max-w-[10rem] truncate rounded-md bg-slate-elevation1 px-2 py-1 font-mono text-xs"
            >
              {parameter.key}
            </span>
          ))}
          {hidden > 0 ? (
            <span className="px-1 py-1 text-xs text-muted-foreground">
              +{hidden} more
            </span>
          ) : null}
        </div>
      )}
    </div>
  );
}

export { InputsCountBadge, WorkflowInputsCard };
