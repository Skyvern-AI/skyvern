import { FocusScope } from "@radix-ui/react-focus-scope";
import { ReloadIcon } from "@radix-ui/react-icons";

import {
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { cn } from "@/util/utils";

import { CodeEditor } from "../components/CodeEditor";
import { YamlModeToggle } from "./YamlModeToggle";

type Props = {
  // "fullscreen" (legacy editor): modal overlay covering the whole editor —
  // dialog semantics + focus trap. "pane" (studio): swaps the Editor pane's
  // content; sibling panes stay usable, so no modal semantics or trap.
  variant?: "fullscreen" | "pane";
};

// YAML editing surface, shown while the store is active. Switching back to
// Visual commits the draft into the graph; broken YAML blocks the switch.
function WorkflowYamlEditor({ variant = "fullscreen" }: Props) {
  const draft = useWorkflowYamlEditorStore((s) => s.draft);
  const error = useWorkflowYamlEditorStore((s) => s.error);
  const committing = useWorkflowYamlEditorStore((s) => s.committing);
  const setDraft = useWorkflowYamlEditorStore((s) => s.setDraft);
  const fullscreen = variant === "fullscreen";

  const switchToVisual = () => {
    void commitYamlDraft(false);
  };

  const surface = (
    <div
      {...(fullscreen
        ? { role: "dialog", "aria-modal": true }
        : { role: "region" })}
      aria-label="Workflow YAML editor"
      className={cn(
        "absolute inset-0 flex flex-col bg-slate-elevation1",
        // The pane variant sits under stage-level overlays (Inputs/Schedule
        // panels at z-40) but above everything inside the Editor pane.
        fullscreen ? "z-50" : "z-30",
      )}
      onKeyDown={(event) => {
        // Standard dialog escape hatch; commit-on-switch means Escape
        // behaves exactly like the Visual toggle (invalid YAML stays open).
        if (event.key === "Escape" && !committing) {
          event.stopPropagation();
          switchToVisual();
        }
      }}
    >
      <div className="flex items-center justify-between gap-3 border-b border-border bg-slate-elevation2 px-4 py-2">
        <div className="flex min-w-0 items-center gap-3">
          <span className="truncate text-xs text-muted-foreground dark:text-slate-500">
            Editing the workflow definition · switch to Visual to apply, then
            Save
          </span>
          {committing ? (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <ReloadIcon className="size-3 animate-spin" />
              Applying…
            </span>
          ) : null}
        </div>
        {/* The studio's pane variant hosts this toggle in the pane header. */}
        {fullscreen ? (
          <YamlModeToggle
            mode="code"
            onVisual={switchToVisual}
            disabled={committing}
          />
        ) : null}
      </div>
      {error ? (
        <div
          role="alert"
          className="border-b border-red-300 bg-red-100 px-4 py-2 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/60 dark:text-red-200"
        >
          <strong className="font-semibold">Invalid YAML:</strong> {error}
        </div>
      ) : null}
      <div className="min-h-0 flex-1">
        <CodeEditor
          language="yaml"
          value={draft}
          onChange={setDraft}
          readOnly={committing}
          fullHeight
          lineWrap={false}
          className="h-full"
          ariaLabel="Workflow definition YAML editor"
          autoFocus
        />
      </div>
    </div>
  );

  if (!fullscreen) {
    return surface;
  }

  return (
    // Trap focus inside the editor while it's open (the canvas is visually
    // covered but still in the tab order); CodeEditor autoFocus lands the
    // caret, so prevent FocusScope from stealing focus to the toggle.
    <FocusScope
      asChild
      loop
      trapped
      onMountAutoFocus={(e) => e.preventDefault()}
    >
      {surface}
    </FocusScope>
  );
}

export { WorkflowYamlEditor };
