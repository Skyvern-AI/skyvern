import { useWorkflowDocEditorFlag } from "@/hooks/useWorkflowDocEditorFlag";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import {
  useWorkflowPresentationStore,
  type WorkflowPresentation,
} from "@/store/WorkflowPresentationStore";
import {
  CodeIcon,
  FileTextIcon,
  EyeOpenIcon,
  MagnifyingGlassIcon,
} from "@radix-ui/react-icons";
import { useEffect, useMemo, useState } from "react";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { WorkflowBlockIcon } from "@/routes/workflows/editor/nodes/WorkflowBlockIcon";
import {
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { filterBlockSearchTargets } from "./blockSearch";
import { PANE_HEADER_ICON_BUTTON_CLASS, studioPanelId } from "./constants";
import { ControlTooltip } from "./ControlTooltip";
import { useStudioPaneCompact } from "./StudioShellContext";
import { ViewToggle } from "./ViewToggle";

/**
 * Editor pane header chrome uses the segmented ViewToggle controls. Entry is registered by the
 * embedded Workspace (it owns the canvas→YAML serialization); exit is the
 * store's shared commit-on-switch flow. Hidden for read-only (global)
 * workflows, which never register an entry point.
 */
export function EditorPaneModeToggle() {
  const compact = useStudioPaneCompact();
  const docEnabled = useWorkflowDocEditorFlag();
  const workflowPermanentId = useWorkflowPermanentId() ?? null;
  const storedPresentation = useWorkflowPresentationStore(
    (s) => s.presentation,
  );
  // Version comparison replaces the canvas, so the Doc can't show there.
  const comparing = useWorkflowPanelStore((s) =>
    Boolean(s.workflowPanelState.data?.showComparison),
  );
  const presentation = comparing ? "visual" : storedPresentation;
  useEffect(() => {
    useWorkflowPresentationStore
      .getState()
      .syncScope(workflowPermanentId, docEnabled);
  }, [workflowPermanentId, docEnabled]);
  const active = useWorkflowYamlEditorStore((s) => s.active);
  const committing = useWorkflowYamlEditorStore((s) => s.committing);
  const enterYamlMode = useWorkflowYamlEditorStore((s) => s.enterYamlMode);
  const switchPresentation = async (next: WorkflowPresentation) => {
    if (committing || (!active && storedPresentation === next)) return;
    const generation = useWorkflowPresentationStore.getState().generation;
    if (active && !(await commitYamlDraft(false))) return;
    const state = useWorkflowPresentationStore.getState();
    // Navigation or flag changes during a commit invalidate the old click.
    if (state.generation === generation) state.setPresentation(next);
  };
  if (!enterYamlMode) {
    return null;
  }
  return (
    <>
      <div
        role="group"
        aria-label="Editor view"
        className="flex shrink-0 items-center gap-1"
      >
        <ViewToggle
          active={!active && (!docEnabled || presentation === "visual")}
          onClick={() => void switchPresentation("visual")}
          disabled={committing}
          compact={compact}
          label="Visual"
          icon={<EyeOpenIcon className="h-3 w-3" />}
        />
        <ViewToggle
          active={active}
          onClick={() => {
            if (!active) {
              enterYamlMode();
            }
          }}
          disabled={committing}
          compact={compact}
          label="YAML"
          icon={<CodeIcon className="h-3 w-3" />}
        />
        {docEnabled && !comparing && (
          <ViewToggle
            active={!active && presentation === "doc"}
            onClick={() => void switchPresentation("doc")}
            disabled={committing}
            compact={compact}
            label="Doc"
            icon={<FileTextIcon className="h-3 w-3" />}
          />
        )}
      </div>
    </>
  );
}

/**
 * Block search for long flows: a magnifier action opening a label-filtered
 * list; picking an entry selects the block and centers the canvas on it via
 * the handle the embedded FlowRenderer registers. Hidden while no editable
 * canvas is mounted (read-only workflows) or while Code mode covers it.
 */
export function EditorPaneBlockSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [paneBoundary, setPaneBoundary] = useState<Element | null>(null);
  const handle = useWorkflowBlockSearchStore((s) => s.handle);
  const yamlModeActive = useWorkflowYamlEditorStore((s) => s.active);
  const presentation = useWorkflowPresentationStore((s) => s.presentation);
  const comparing = useWorkflowPanelStore((s) =>
    Boolean(s.workflowPanelState.data?.showComparison),
  );
  const targets = useMemo(
    () => (open && handle ? handle.getTargets() : []),
    [open, handle],
  );
  if (!handle || yamlModeActive || (presentation === "doc" && !comparing)) {
    return null;
  }
  const results = filterBlockSearchTargets(targets, query);
  const closeAndReset = () => {
    setOpen(false);
    setQuery("");
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (next) {
          // Clamp the popover to the Editor pane so a narrow pane cannot
          // overflow it (the content is portalled, so Radix only knows the
          // boundary we hand it).
          setPaneBoundary(document.getElementById(studioPanelId("editor")));
          setOpen(true);
          return;
        }
        closeAndReset();
      }}
    >
      <ControlTooltip content="Search blocks">
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label="Search blocks"
            className={PANE_HEADER_ICON_BUTTON_CLASS}
          >
            <MagnifyingGlassIcon className="h-3.5 w-3.5" />
          </button>
        </PopoverTrigger>
      </ControlTooltip>
      <PopoverContent
        align="end"
        sideOffset={6}
        collisionBoundary={paneBoundary}
        collisionPadding={8}
        className="w-64 max-w-[var(--radix-popper-available-width)] p-0"
      >
        <Command
          shouldFilter={false}
          onKeyDown={(event) => {
            // Escape only dismisses the popover; without the stop it bubbles
            // to the canvas's window handler and clears the block selection.
            if (event.key === "Escape") {
              event.stopPropagation();
              closeAndReset();
            }
          }}
        >
          <CommandInput
            placeholder="Search blocks…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            <CommandEmpty>No blocks found.</CommandEmpty>
            {results.length > 0 ? (
              <CommandGroup>
                {results.map((target) => (
                  <CommandItem
                    key={target.nodeId}
                    value={target.nodeId}
                    onSelect={() => {
                      handle.focusBlock(target.nodeId);
                      closeAndReset();
                    }}
                  >
                    <span className="mr-2 flex h-4 w-4 shrink-0 items-center justify-center text-muted-foreground">
                      {target.blockType ? (
                        <WorkflowBlockIcon
                          workflowBlockType={target.blockType}
                          className="h-4 w-4"
                        />
                      ) : null}
                    </span>
                    <span className="truncate">{target.label}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
