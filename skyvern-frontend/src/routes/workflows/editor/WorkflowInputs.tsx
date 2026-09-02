import { InputIcon, PlusIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { cn } from "@/util/utils";

// The node is a summary; the panel lists every input. Six names fill about
// two lines of the 30rem card. Keys have no length limit, so each is cut to
// what the panel's 12rem name column shows; the full sentence stays in the
// accessible name and the tooltip.
const MAX_VISIBLE_KEYS = 6;
const MAX_KEY_CHARS = 24;

// The editor's hand-rolled controls all carry this ring.
const FOCUS_RING =
  "rounded-md focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50";

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

function summarize(keys: string[], maxKeyChars = Infinity): string {
  const hidden = keys.length - MAX_VISIBLE_KEYS;
  const shown = keys
    .slice(0, MAX_VISIBLE_KEYS)
    .map((key) =>
      key.length > maxKeyChars ? `${key.slice(0, maxKeyChars - 1)}\u2026` : key,
    )
    .join(", ");
  return hidden > 0 ? `${shown}, and ${hidden} more` : shown;
}

/**
 * Inputs, stated where the flow starts, because a header toggle was the only
 * door and nobody found it (SKY-14866). A section of the Start card under its
 * heading, beside Workflow Settings rather than inside it: that accordion
 * already carries thirteen configs and would hide Inputs again.
 *
 * A summary in plain language, not an editor: the names read as a sentence in
 * the body font, and the heading, the sentence and Add all open the existing
 * Inputs panel. The heading row copies the sibling accordion trigger's grammar
 * (h3 wrapping a button, label underlines on hover) so the two rows behave
 * alike. `editable` is the start node's own flag (false for global workflows
 * and deleted snapshots, matching what the editor headers hide). The summary
 * still renders when it is false, but nothing is clickable, because
 * WorkflowParametersPanel can mutate and would dirty a workflow that cannot be
 * saved.
 */
function WorkflowInputsSection({ editable }: { editable: boolean }) {
  const parameters = useWorkflowParametersStore((s) => s.parameters);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );
  const openPanel = () =>
    setWorkflowPanelState({ active: true, content: "parameters" });

  // Nothing to summarise and nothing to offer.
  if (!editable && parameters.length === 0) {
    return null;
  }

  const heading = (
    <>
      <InputIcon className="size-4 text-muted-foreground" aria-hidden />
      <span className={cn({ "group-hover:underline": editable })}>Inputs</span>
      <InputsCountBadge count={parameters.length} />
    </>
  );
  const keys = parameters.map((parameter) => parameter.key);
  const summary = summarize(keys, MAX_KEY_CHARS);
  const fullSummary = summarize(keys);

  return (
    // Clicks stop here: a canvas click on the start node expands Workflow
    // Settings (FlowRenderer's onNodeClick), and reading a name must not. pl-6
    // aligns with the settings trigger's label, which sits after its chevron.
    <div
      className="nodrag nopan mt-3 pl-6 text-left"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="flex min-w-0">
          {editable ? (
            <button
              type="button"
              disabled={isRecording}
              onClick={openPanel}
              className={cn(
                "group flex items-center gap-2 py-1 text-sm font-medium",
                FOCUS_RING,
              )}
            >
              {heading}
            </button>
          ) : (
            <span className="flex items-center gap-2 py-1 text-sm font-medium">
              {heading}
            </span>
          )}
        </h3>
        {editable ? (
          <Button
            variant="ghost"
            size="sm"
            className="-mr-3 shrink-0 text-muted-foreground hover:text-foreground"
            disabled={isRecording}
            onClick={openPanel}
          >
            <PlusIcon className="mr-1 size-3.5" aria-hidden />
            Add
          </Button>
        ) : null}
      </div>
      {parameters.length === 0 ? (
        <p className="pl-6 text-sm text-muted-foreground">
          None yet. Add placeholders to fill in before each run.
        </p>
      ) : editable ? (
        <button
          type="button"
          disabled={isRecording}
          onClick={openPanel}
          title={fullSummary}
          aria-label={fullSummary}
          className={cn(
            "ml-6 block break-words text-left text-sm hover:underline",
            FOCUS_RING,
          )}
        >
          {summary}
        </button>
      ) : (
        // aria-label is not allowed on a paragraph, so the full text rides
        // along visually hidden instead.
        <p className="break-words pl-6 text-sm" title={fullSummary}>
          <span aria-hidden>{summary}</span>
          <span className="sr-only">{fullSummary}</span>
        </p>
      )}
    </div>
  );
}

export { InputsCountBadge, WorkflowInputsSection };
