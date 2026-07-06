import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { useMemo, useState } from "react";

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

import { YamlModeToggle } from "../editor/YamlModeToggle";
import { filterBlockSearchTargets } from "./blockSearch";
import { PANE_HEADER_ICON_BUTTON_CLASS, studioPanelId } from "./constants";
import { ControlTooltip } from "./ControlTooltip";
import { PaneHeaderDivider } from "./PaneHeaderDivider";
import { useStudioPaneCompact } from "./StudioShellContext";

/**
 * Editor pane header chrome: the Visual/Code mode toggle, relocated from the
 * canvas's floating overlay. Entry is registered by the embedded Workspace
 * (it owns the canvas→YAML serialization); exit is the store's shared
 * commit-on-switch flow. Hidden for read-only (global) workflows, which never
 * register an entry point.
 */
export function EditorPaneModeToggle() {
  const compact = useStudioPaneCompact();
  const active = useWorkflowYamlEditorStore((s) => s.active);
  const committing = useWorkflowYamlEditorStore((s) => s.committing);
  const enterYamlMode = useWorkflowYamlEditorStore((s) => s.enterYamlMode);
  if (!enterYamlMode) {
    return null;
  }
  return (
    <>
      <PaneHeaderDivider />
      <YamlModeToggle
        mode={active ? "code" : "visual"}
        compact={compact}
        disabled={committing}
        onCode={enterYamlMode}
        onVisual={() => void commitYamlDraft(false)}
      />
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
  const targets = useMemo(
    () => (open && handle ? handle.getTargets() : []),
    [open, handle],
  );
  if (!handle || yamlModeActive) {
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
