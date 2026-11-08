import { PauseIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import type { DemonstrationStatus } from "@/store/useRecordingLauncherStore";

// The Copilot composer's recording bar, drawn where the recorded steps will land. Done and Discard stay in Copilot.
export function DocDemonstrationLine({
  status,
}: {
  status: DemonstrationStatus;
}) {
  const recording = status.phase === "recording";
  return (
    <div
      role="status"
      data-doc-demonstration
      className={cn(
        "flex min-h-9 items-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium",
        recording
          ? "border-[#a16207]/30 bg-[#a16207]/[0.14]"
          : "border-border text-muted-foreground",
      )}
    >
      {recording ? (
        <span
          aria-hidden
          className="h-2 w-2 shrink-0 rounded-full bg-red-500"
        />
      ) : (
        <PauseIcon
          aria-hidden
          className="h-3 w-3 shrink-0 text-muted-foreground"
        />
      )}
      <span>
        {recording
          ? "Demonstrating"
          : status.interrupted
            ? "Demonstration interrupted"
            : "Demonstration paused"}
        {` · ${status.actionCount} ${status.actionCount === 1 ? "action" : "actions"}`}
      </span>
    </div>
  );
}
