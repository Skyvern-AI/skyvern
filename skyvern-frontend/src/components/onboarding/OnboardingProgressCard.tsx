import { ReloadIcon } from "@radix-ui/react-icons";

import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

type ProgressActionKey = "first_agent_created" | "first_successful_run";

type ActiveProgressCardProps = {
  state: "active";
  completedCount: 0 | 1;
  nextActionKey: ProgressActionKey;
  firstMilestoneComplete: boolean;
  isPending: boolean;
  onDismiss: () => void;
};

type DismissedProgressCardProps = {
  state: "dismissed";
  isPending: boolean;
  onRestore: () => void;
};

type OnboardingProgressCardProps =
  | ActiveProgressCardProps
  | DismissedProgressCardProps;

const primaryActions = {
  first_agent_created: {
    label: "Use the working example",
    href: "#working-example-heading",
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
} satisfies Record<
  ProgressActionKey,
  {
    label: string;
    href: string;
    resourceLabel: string;
    resourceHref: string;
  }
>;

function OnboardingProgressCard(props: OnboardingProgressCardProps) {
  const activeProgress = props.state === "active" ? props : null;
  const primaryAction = activeProgress
    ? primaryActions[activeProgress.nextActionKey]
    : null;
  const firstMilestoneComplete =
    activeProgress?.firstMilestoneComplete === true;
  const handleVisibilityChange = () => {
    if (props.isPending) return;
    if (props.state === "active") {
      props.onDismiss();
      return;
    }
    props.onRestore();
  };

  return (
    <div
      className={
        activeProgress
          ? "relative rounded-lg border border-border bg-card px-4 py-4 shadow-sm sm:px-5"
          : "flex justify-end"
      }
    >
      <Button
        type="button"
        variant={props.isPending ? "disabled" : "ghost"}
        className={`h-11 touch-manipulation text-muted-foreground hover:text-foreground ${
          activeProgress ? "absolute right-4 top-4 sm:right-5" : ""
        }`}
        aria-disabled={props.isPending}
        aria-busy={props.isPending}
        onClick={handleVisibilityChange}
      >
        {props.isPending && (
          <ReloadIcon
            aria-hidden="true"
            className="mr-2 h-4 w-4 motion-safe:animate-spin motion-reduce:animate-none"
          />
        )}
        {activeProgress ? "Hide setup" : "Resume setup"}
      </Button>

      {activeProgress && primaryAction && (
        <section
          aria-labelledby="onboarding-progress-heading"
          aria-busy={activeProgress.isPending}
        >
          <header className="pr-28">
            <h2
              id="onboarding-progress-heading"
              className="text-base font-semibold tracking-tight"
            >
              Build your first agent
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              <span className="font-semibold text-foreground">
                {activeProgress.completedCount} of 2
              </span>{" "}
              complete
            </p>
          </header>

          <ol
            aria-label="Setup milestones"
            className="mt-4 grid gap-2 sm:grid-cols-2"
          >
            <li className="flex min-h-11 items-center gap-2 rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-sm">
              <span
                aria-hidden="true"
                className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                  firstMilestoneComplete
                    ? "bg-badge-success text-foreground"
                    : "border border-border text-muted-foreground"
                }`}
              >
                {firstMilestoneComplete ? "✓" : "1"}
              </span>
              <span
                className={
                  firstMilestoneComplete
                    ? "text-muted-foreground"
                    : "font-medium text-foreground"
                }
              >
                {firstMilestoneComplete && (
                  <span className="sr-only">Complete: </span>
                )}
                Create your first agent
              </span>
            </li>
            <li className="flex min-h-11 items-center gap-2 rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-sm">
              <span
                aria-hidden="true"
                className="flex size-6 shrink-0 items-center justify-center rounded-full border border-border text-muted-foreground"
              >
                2
              </span>
              <span className="font-medium text-foreground">
                Run your first agent
              </span>
            </li>
          </ol>

          <div className="mt-4 flex flex-col items-start gap-2 sm:flex-row sm:items-center">
            <Button
              asChild
              className="h-11 w-full touch-manipulation sm:w-auto"
            >
              {activeProgress.nextActionKey === "first_successful_run" ? (
                <Link to={primaryAction.href}>{primaryAction.label}</Link>
              ) : (
                <a
                  href={primaryAction.href}
                  onClick={() => {
                    document
                      .getElementById("working-example-heading")
                      ?.focus({ preventScroll: true });
                  }}
                >
                  {primaryAction.label}
                </a>
              )}
            </Button>
            <a
              href={primaryAction.resourceHref}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-medium text-muted-foreground underline underline-offset-4 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {primaryAction.resourceLabel}
            </a>
          </div>
        </section>
      )}
    </div>
  );
}

export { OnboardingProgressCard };
