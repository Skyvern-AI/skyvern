import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRunViewingPreferenceStore } from "@/store/RunViewingPreferenceStore";
import { cn } from "@/util/utils";
import { RowsIcon, ViewVerticalIcon } from "@radix-ui/react-icons";

type ModeButtonProps = {
  active: boolean;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
};

function ModeButton({ active, label, onClick, children }: ModeButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={label}
          aria-pressed={active}
          onClick={onClick}
          className={cn(
            "flex h-6 w-7 items-center justify-center rounded-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-brand",
            {
              "bg-slate-elevation4 text-foreground": active,
            },
          )}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function RunViewingModeToggle() {
  const viewMode = useRunViewingPreferenceStore((s) => s.viewMode);
  const setViewMode = useRunViewingPreferenceStore((s) => s.setViewMode);

  return (
    <TooltipProvider delayDuration={300}>
      <div
        role="group"
        aria-label="Action list view density"
        data-slot="run-viewing-mode-toggle"
        className="flex h-7 items-center gap-0.5 rounded-md border bg-slate-elevation2 p-0.5"
      >
        <ModeButton
          active={viewMode === "compact"}
          label="Compact view"
          onClick={() => setViewMode("compact")}
        >
          <RowsIcon className="h-3.5 w-3.5" />
        </ModeButton>
        <ModeButton
          active={viewMode === "detailed"}
          label="Detailed view"
          onClick={() => setViewMode("detailed")}
        >
          <ViewVerticalIcon className="h-3.5 w-3.5" />
        </ModeButton>
      </div>
    </TooltipProvider>
  );
}

export { RunViewingModeToggle };
