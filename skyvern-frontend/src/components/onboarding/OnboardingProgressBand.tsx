import { CheckIcon, ExternalLinkIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";

type ProgressActionKey = "first_agent_created" | "first_successful_run";
type Progress = {
  state: string;
  completed_count?: number;
  next_action_key?: ProgressActionKey | null;
  items?: Array<{
    key: ProgressActionKey;
    completed_at: string | null;
  }>;
};
type ActiveProgress = Progress & {
  state: "active";
  completed_count: 0 | 1;
  next_action_key: ProgressActionKey;
  items: NonNullable<Progress["items"]>;
};
type OnboardingProgressBandProps = {
  progress: Progress | null;
  isPending: boolean;
  onDismiss: () => void;
  onRestore: () => void;
  onDescribeAgent: () => void;
  trackRemaining?: number;
  onDismissHandoff?: () => void;
  trackDismissed?: boolean;
  trackPending?: boolean;
  onRestoreTrack?: () => void;
  children?: ReactNode;
};
type StepState = "complete" | "active" | "pending" | "upcoming";

const milestoneLabels = [
  "Account created",
  "Create your first agent",
  "Run your first agent",
] as const;

const primaryActions = {
  first_agent_created: {
    label: "Describe your first agent",
    resourceLabel: "How to build an agent",
    resourceHref:
      "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
  },
  first_successful_run: {
    label: "Run agent",
    href: "/agents",
    resourceLabel: "How to run an agent",
    resourceHref:
      "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  },
} as const;

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
  totalSteps = milestoneLabels.length,
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

function SetupRow({
  label,
  index,
  state,
  affordance,
  secondaryAffordance,
}: {
  label: string;
  index: number;
  state: StepState;
  affordance?: ReactNode;
  secondaryAffordance?: ReactNode;
}) {
  const isComplete = state === "complete";
  return (
    <li className="grid min-h-11 grid-cols-[auto_1fr_2.75rem] items-center gap-3 rounded-md border border-border/70 bg-background/70 px-3 py-2 text-sm">
      <StepDot state={state} index={index} />
      <div>
        <span
          className={
            isComplete ? "text-muted-foreground" : "font-medium text-foreground"
          }
        >
          <span className="sr-only">
            {isComplete
              ? "Complete: "
              : state === "active"
                ? "Current step: "
                : state === "pending"
                  ? "Saving current step: "
                  : "Upcoming step: "}
          </span>
          {label}
        </span>
        {secondaryAffordance}
      </div>
      <span className="flex min-h-11 items-center justify-end">
        {affordance}
      </span>
    </li>
  );
}

function QuietResourceLink({
  href,
  label,
  isPending,
}: {
  href: string;
  label: string;
  isPending: boolean;
}) {
  if (isPending) {
    return (
      <Button
        type="button"
        variant="disabled"
        disabled
        aria-label={label}
        className="size-11 p-0"
      >
        <ExternalLinkIcon aria-hidden="true" className="size-4" />
      </Button>
    );
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={label}
      aria-label={label}
      className="inline-flex size-11 touch-manipulation items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      <ExternalLinkIcon aria-hidden="true" className="size-4" />
    </a>
  );
}

function ProgressPrimaryAction({
  nextActionKey,
  isPending,
  onDescribeAgent,
}: {
  nextActionKey: ProgressActionKey;
  isPending: boolean;
  onDescribeAgent: () => void;
}) {
  const action = primaryActions[nextActionKey];
  if (isPending) {
    return (
      <Button
        type="button"
        variant="disabled"
        disabled
        className="h-11 w-full sm:w-auto"
      >
        {action.label}
      </Button>
    );
  }

  if (nextActionKey === "first_agent_created") {
    return (
      <Button
        type="button"
        className="h-11 w-full touch-manipulation sm:w-auto"
        onClick={onDescribeAgent}
      >
        {action.label}
      </Button>
    );
  }

  return (
    <Button asChild className="h-11 w-full touch-manipulation sm:w-auto">
      <Link to={primaryActions.first_successful_run.href}>{action.label}</Link>
    </Button>
  );
}

function WorkingExampleAffordance() {
  return (
    <a
      href="#working-example-heading"
      className="ml-2 inline-flex min-h-11 touch-manipulation items-center text-xs font-normal text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      onClick={() => {
        document
          .getElementById("working-example-heading")
          ?.focus({ preventScroll: true });
      }}
    >
      or copy a working example
    </a>
  );
}

function HandoffRow({
  remaining,
  onDismiss,
}: {
  remaining: number;
  onDismiss?: () => void;
}) {
  return (
    <section
      aria-labelledby="onboarding-handoff-heading"
      aria-live="polite"
      role="status"
      className="flex min-h-11 flex-wrap items-center gap-3 rounded-lg border border-border bg-background px-5 py-3 shadow-sm motion-safe:animate-in motion-safe:fade-in motion-reduce:animate-none sm:px-6"
    >
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-badge-success text-foreground">
        <CheckIcon aria-hidden="true" className="size-3.5" />
      </span>
      <p
        id="onboarding-handoff-heading"
        className="text-sm font-semibold tracking-tight"
      >
        First agent ready.
      </p>
      <Link
        to="/getting-started"
        onClick={onDismiss}
        className="inline-flex min-h-11 touch-manipulation items-center text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        Keep going: {remaining} more {remaining === 1 ? "step" : "steps"} →
      </Link>
      <Button
        type="button"
        variant="ghost"
        className="ml-auto h-11 touch-manipulation text-muted-foreground hover:text-foreground"
        onClick={onDismiss}
      >
        Hide
      </Button>
    </section>
  );
}

// A hidden track has no sidebar row and no handoff, so Home keeps one quiet
// way back to it.
function ResumeTrackRow({
  isPending,
  onRestore,
}: {
  isPending: boolean;
  onRestore?: () => void;
}) {
  return (
    <div className="flex justify-end">
      <Button
        type="button"
        variant={isPending ? "disabled" : "ghost"}
        className="h-11 touch-manipulation text-muted-foreground hover:text-foreground"
        aria-disabled={isPending}
        aria-busy={isPending}
        onClick={() => {
          if (!isPending) onRestore?.();
        }}
      >
        Resume getting started
      </Button>
    </div>
  );
}

function CompletionBand() {
  return (
    <section
      aria-labelledby="onboarding-progress-heading"
      aria-live="polite"
      role="status"
      className="overflow-hidden rounded-lg border border-border bg-background shadow-sm"
    >
      <div className="border-b border-border bg-card px-5 py-4 sm:px-6">
        <div className="flex items-center gap-3">
          <span className="flex size-8 items-center justify-center rounded-full bg-badge-success text-foreground motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-75 motion-reduce:animate-none">
            <CheckIcon aria-hidden="true" className="size-4" />
          </span>
          <h2
            id="onboarding-progress-heading"
            className="text-base font-semibold tracking-tight"
          >
            First agent ready
          </h2>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          <span className="sr-only">Complete: </span>3 of 3 complete
        </p>
        <div className="mt-3">
          <ProgressSegments completedSteps={3} />
        </div>
      </div>
    </section>
  );
}

function isActiveProgress(
  progress: Progress | null,
): progress is ActiveProgress {
  return (
    progress?.state === "active" &&
    (progress.completed_count === 0 || progress.completed_count === 1) &&
    (progress.next_action_key === "first_agent_created" ||
      progress.next_action_key === "first_successful_run") &&
    Array.isArray(progress.items)
  );
}

function OnboardingProgressBand({
  progress,
  isPending,
  onDismiss,
  onRestore,
  onDescribeAgent,
  trackRemaining = 0,
  onDismissHandoff,
  trackDismissed = false,
  trackPending = false,
  onRestoreTrack,
  children,
}: OnboardingProgressBandProps) {
  const previousState = useRef(progress?.state);
  const [isCelebrating, setIsCelebrating] = useState(false);
  // Completed progress with an active track keeps a compact handoff in place
  // of the retired band; the timed celebration only remains for a null track.
  const showHandoff = progress?.state === "completed" && trackRemaining > 0;

  useEffect(() => {
    const completedFromActive =
      previousState.current === "active" && progress?.state === "completed";
    previousState.current = progress?.state;
    if (!completedFromActive || showHandoff) {
      setIsCelebrating(false);
      return;
    }

    setIsCelebrating(true);
    const timeout = window.setTimeout(() => setIsCelebrating(false), 1800);
    return () => window.clearTimeout(timeout);
  }, [progress?.state, showHandoff]);

  const resumeTrack = trackDismissed ? (
    <ResumeTrackRow isPending={trackPending} onRestore={onRestoreTrack} />
  ) : null;
  if (showHandoff) {
    return (
      <HandoffRow remaining={trackRemaining} onDismiss={onDismissHandoff} />
    );
  }
  if (isCelebrating) return <CompletionBand />;

  const activeProgress = isActiveProgress(progress) ? progress : null;
  const isDismissed = progress?.state === "dismissed";
  if (!activeProgress && !isDismissed) return resumeTrack;

  const completedSteps = activeProgress
    ? activeProgress.completed_count + 1
    : 0;
  const action = activeProgress
    ? primaryActions[activeProgress.next_action_key]
    : null;
  const activeIndex = activeProgress?.items.some(
    (item) => item.key === "first_agent_created" && item.completed_at !== null,
  )
    ? 2
    : 1;

  return (
    <>
      <div className={activeProgress ? "relative" : "flex justify-end"}>
        <Button
          type="button"
          variant={isPending ? "disabled" : "ghost"}
          className={`z-10 h-11 touch-manipulation text-muted-foreground hover:text-foreground ${
            activeProgress ? "absolute right-5 top-4" : ""
          }`}
          aria-disabled={isPending}
          aria-busy={isPending}
          onClick={() => {
            if (isPending) return;
            if (activeProgress) onDismiss();
            else onRestore();
          }}
        >
          {isPending && <PendingIcon />}
          {activeProgress ? "Hide setup" : "Resume setup"}
        </Button>

        {activeProgress && action ? (
          <section
            aria-labelledby="onboarding-progress-heading"
            aria-busy={isPending}
            className="overflow-hidden rounded-lg border border-border bg-background shadow-sm"
          >
            {isPending ? (
              <span className="sr-only" aria-live="polite">
                Saving setup progress…
              </span>
            ) : null}
            <div className="border-b border-border bg-card px-5 py-4 sm:px-6">
              <div className="pr-28">
                <h2
                  id="onboarding-progress-heading"
                  className="text-base font-semibold tracking-tight"
                >
                  Build your first agent
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  An agent is a browser automation you describe in plain words.
                </p>
                <p className="mt-1 text-sm text-muted-foreground">
                  <span className="font-semibold tabular-nums text-foreground">
                    {completedSteps} of 3
                  </span>{" "}
                  complete
                </p>
              </div>

              <div className="mt-4">
                <ProgressSegments completedSteps={completedSteps} />
              </div>

              <ol aria-label="Setup milestones" className="mt-4 grid gap-2">
                {milestoneLabels.map((label, index) => {
                  const state: StepState =
                    index < completedSteps
                      ? "complete"
                      : isPending && index === activeIndex
                        ? "pending"
                        : index === activeIndex
                          ? "active"
                          : "upcoming";
                  return (
                    <SetupRow
                      key={label}
                      label={label}
                      index={index}
                      state={state}
                      secondaryAffordance={
                        index === 1 &&
                        activeProgress.next_action_key ===
                          "first_agent_created" ? (
                          <WorkingExampleAffordance />
                        ) : undefined
                      }
                      affordance={
                        index === activeIndex ? (
                          <QuietResourceLink
                            href={action.resourceHref}
                            label={action.resourceLabel}
                            isPending={isPending}
                          />
                        ) : undefined
                      }
                    />
                  );
                })}
              </ol>

              <div className="mt-4 flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
                <ProgressPrimaryAction
                  nextActionKey={activeProgress.next_action_key}
                  isPending={isPending}
                  onDescribeAgent={onDescribeAgent}
                />
              </div>
            </div>

            {children && (
              <div className="[&>section]:rounded-none [&>section]:border-0">
                {children}
              </div>
            )}
          </section>
        ) : null}
      </div>
      {resumeTrack}
    </>
  );
}

export { OnboardingProgressBand, PendingIcon, ProgressSegments, StepDot };
export type { StepState };
