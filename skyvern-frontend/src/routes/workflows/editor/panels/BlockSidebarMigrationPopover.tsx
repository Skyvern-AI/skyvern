import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useBlockSidebarOnboardingStore } from "@/store/BlockSidebarOnboardingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { useWorkflowEditorMode } from "../hooks/useWorkflowEditorMode";

function BlockSidebarMigrationPopover() {
  const mode = useWorkflowEditorMode();
  const selectedBlockId = useWorkflowPanelStore((s) => s.selectedBlockId);
  const hasSeenMigration = useBlockSidebarOnboardingStore(
    (s) => s.hasSeenMigration,
  );
  const markSeen = useBlockSidebarOnboardingStore((s) => s.markSeen);

  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (mode === "edit" && selectedBlockId !== null && !hasSeenMigration) {
      setOpen(true);
    } else {
      setOpen(false);
    }
  }, [mode, selectedBlockId, hasSeenMigration]);

  if (!open) {
    return null;
  }

  return (
    <div
      data-testid="block-sidebar-migration-popover"
      role="dialog"
      aria-live="polite"
      className="absolute right-[calc(var(--block-sidebar-w)+4.5rem)] top-32 z-40 w-72 rounded-xl border border-border bg-slate-elevation3 p-4 shadow-xl"
    >
      <h3 className="text-sm font-medium text-slate-100">
        Configuration moved here
      </h3>
      <p className="mt-1 text-xs text-slate-400">
        Block settings now live in this side panel. Edits save automatically -
        no need to confirm. You can drag the panel's left edge to make it wider.
      </p>
      <div className="mt-3 flex justify-end">
        <Button
          size="sm"
          variant="tertiary"
          onClick={() => {
            markSeen();
          }}
        >
          Got it
        </Button>
      </div>
    </div>
  );
}

export { BlockSidebarMigrationPopover };
