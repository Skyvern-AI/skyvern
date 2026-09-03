import { ReloadIcon } from "@radix-ui/react-icons";

type StepState = "complete" | "active" | "pending" | "upcoming";

function PendingIcon() {
  return (
    <ReloadIcon
      aria-hidden="true"
      className="mr-2 h-4 w-4 motion-safe:animate-spin motion-reduce:animate-none"
    />
  );
}

function StepDot({ state, index }: { state: StepState; index: number }) {
  const isComplete = state === "complete";
  return (
    <span
      aria-hidden="true"
      className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
        isComplete
          ? "bg-badge-success text-foreground"
          : state === "active"
            ? "border border-primary bg-primary/10 text-primary"
            : state === "pending"
              ? "border border-primary/50 bg-primary/5 text-primary"
              : "border border-border text-muted-foreground"
      }`}
    >
      {isComplete ? "✓" : index + 1}
    </span>
  );
}

function ProgressSegments({
  completedSteps,
  totalSteps = 3,
  label = "Setup progress",
}: {
  completedSteps: number;
  totalSteps?: number;
  label?: string;
}) {
  return (
    <div
      className="grid gap-1"
      style={{ gridTemplateColumns: `repeat(${totalSteps}, minmax(0, 1fr))` }}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={totalSteps}
      aria-valuenow={completedSteps}
    >
      {Array.from({ length: totalSteps }, (_, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={`h-1.5 rounded-full ${
            index < completedSteps ? "bg-primary" : "bg-muted"
          }`}
        />
      ))}
    </div>
  );
}

export { PendingIcon, ProgressSegments, StepDot };
export type { StepState };
