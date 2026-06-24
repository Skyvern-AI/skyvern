import { ChevronRightIcon } from "@radix-ui/react-icons";

export function CopilotRail({ onExpand }: { onExpand: () => void }) {
  return (
    <div className="flex h-full w-full flex-col items-center gap-3 rounded-lg border border-border bg-slate-elevation1 py-3">
      <button
        type="button"
        onClick={onExpand}
        title="Show Copilot"
        aria-label="Show Copilot"
        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <ChevronRightIcon className="h-4 w-4" />
      </button>
      <span className="h-2 w-2 rounded-full bg-studio-accent shadow-[0_0_0_4px_rgba(109,108,246,0.18)]" />
      <span
        className="mt-1 text-xs font-medium tracking-wide text-muted-foreground"
        style={{ writingMode: "vertical-rl" }}
      >
        Copilot
      </span>
    </div>
  );
}
