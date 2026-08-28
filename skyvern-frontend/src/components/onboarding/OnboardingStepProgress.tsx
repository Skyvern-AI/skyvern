import { cn } from "@/util/utils";

type Props = {
  stepIndex: number;
  stepCount: number;
  chip?: "optional" | "final";
};

function OnboardingStepProgress({
  stepIndex,
  stepCount,
  chip,
}: Readonly<Props>) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pr-12">
      <div
        aria-hidden="true"
        data-testid="onboarding-step-dots"
        className="flex items-center gap-1"
      >
        {Array.from({ length: stepCount }, (_, index) => (
          <span
            key={index}
            data-active={index === stepIndex - 1 ? "true" : "false"}
            className={cn(
              "h-1.5 rounded-full",
              index === stepIndex - 1
                ? "w-6 bg-primary"
                : "w-1.5 bg-muted-foreground/40",
            )}
          />
        ))}
      </div>
      <span className="rounded-md bg-muted px-2 py-1 text-[11px] font-medium uppercase tabular-nums tracking-wider text-muted-foreground">
        STEP {stepIndex} OF {stepCount}
      </span>
      {chip ? (
        <span
          className={cn(
            "ml-auto rounded-md px-2 py-1 text-[11px] font-medium uppercase tracking-wider",
            chip === "final"
              ? "border border-primary text-primary"
              : "bg-muted text-muted-foreground",
          )}
        >
          {chip === "final" ? "FINAL STEP" : "OPTIONAL"}
        </span>
      ) : null}
    </div>
  );
}

export { OnboardingStepProgress };
