import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { QuestionnaireAnswersV1 } from "@/store/onboarding/types";
import { OnboardingStepShell } from "./OnboardingStepShell";

type NextStep = {
  id: string;
  title: string;
  description: string;
  actionLabel: string;
  actionHref: string;
  resourceLabel: string;
  resourceHref: string;
};

type Props = {
  answers: QuestionnaireAnswersV1 | null;
  onAction: () => void;
  onBack: () => void;
  onSkip: () => void;
};

const resources = {
  build: "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
  credentials:
    "https://www.skyvern.com/docs/cloud/managing-credentials/credentials-overview",
  mcp: "https://www.skyvern.com/docs/cloud/getting-started/mcp",
  run: "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  schedule: "https://www.skyvern.com/docs/cloud/building-agents/scheduling",
} as const;

function questionnaireNextSteps(
  answers: QuestionnaireAnswersV1 | null,
): readonly NextStep[] | null {
  if (
    !answers?.role ||
    !answers.company_context ||
    !answers.scale_intent ||
    !answers.referral_source
  ) {
    return null;
  }

  const technicalRole =
    answers.role === "developer" || answers.role === "technical_operator";
  const scaledUse =
    answers.scale_intent === "recurring_individual" ||
    answers.scale_intent === "team_or_multi_workflow" ||
    answers.scale_intent === "production_high_volume";

  return [
    scaledUse
      ? {
          id: "build-reusable",
          title: "Build a reusable agent",
          description: "Start from a template, then adapt it for repeat use.",
          actionLabel: "Browse templates",
          actionHref: "/discover",
          resourceLabel: "How to build an agent",
          resourceHref: resources.build,
        }
      : {
          id: "start-template",
          title: "Start with a template",
          description: "Choose a template and tailor it to your first task.",
          actionLabel: "Browse templates",
          actionHref: "/discover",
          resourceLabel: "How to build an agent",
          resourceHref: resources.build,
        },
    technicalRole
      ? {
          id: "connect-stack",
          title: "Connect Skyvern to your stack",
          description:
            "Use an integration to bring browser automation into your tools.",
          actionLabel: "Open integrations",
          actionHref: "/integrations",
          resourceLabel: "Set up MCP",
          resourceHref: resources.mcp,
        }
      : {
          id: "add-credentials",
          title: "Add website credentials",
          description: "Save the logins your agent needs before its first run.",
          actionLabel: "Open credentials",
          actionHref: "/credentials",
          resourceLabel: "How credentials work",
          resourceHref: resources.credentials,
        },
    scaledUse
      ? {
          id: "schedule-runs",
          title: "Schedule repeat runs",
          description:
            "Open your agent when you are ready to set a recurring schedule.",
          actionLabel: "Open agents",
          actionHref: "/agents",
          resourceLabel: "How to schedule an agent",
          resourceHref: resources.schedule,
        }
      : {
          id: "run-agent",
          title: "Run and review your agent",
          description: "Open your agent, start a run, and review the result.",
          actionLabel: "Open agents",
          actionHref: "/agents",
          resourceLabel: "How to run an agent",
          resourceHref: resources.run,
        },
  ];
}

function QuestionnaireNextStepsCard({
  answers,
  onAction,
  onBack,
  onSkip,
}: Readonly<Props>) {
  const steps = questionnaireNextSteps(answers);
  if (!steps) return null;

  return (
    <OnboardingStepShell stepIndex={3} stepCount={3} chip="final">
      <DialogHeader className="pr-12">
        <DialogTitle className="text-xl font-semibold">
          Your next three steps
        </DialogTitle>
        <DialogDescription>
          Based on the answers you just saved, here is a practical path to your
          first successful run.
        </DialogDescription>
      </DialogHeader>
      <ol
        aria-label="Recommended next steps"
        className="flex min-h-0 flex-col gap-3 overflow-y-auto overscroll-contain pr-1"
      >
        {steps.map((step, index) => (
          <li
            key={step.id}
            className="rounded-lg border border-border bg-muted/20 p-3"
          >
            <div className="flex gap-3">
              <span
                aria-hidden="true"
                className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border text-xs font-semibold"
              >
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <h3 className="text-sm font-semibold text-foreground">
                  {step.title}
                </h3>
                <p className="mt-1 text-sm text-muted-foreground">
                  {step.description}
                </p>
                <div className="mt-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
                  <Button
                    asChild
                    variant="outline"
                    size="sm"
                    className="min-h-11 w-full touch-manipulation sm:w-auto"
                  >
                    <Link to={step.actionHref} onClick={onAction}>
                      {step.actionLabel}
                    </Link>
                  </Button>
                  <a
                    href={step.resourceHref}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-medium text-muted-foreground underline underline-offset-4 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    {step.resourceLabel}
                  </a>
                </div>
              </div>
            </div>
          </li>
        ))}
      </ol>
      <DialogFooter className="sticky bottom-0 z-10 -mx-6 -mb-6 flex-col gap-2 border-t border-border bg-background px-6 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4 sm:flex-row sm:justify-between sm:gap-0 sm:space-x-0">
        <Button
          variant="ghost"
          size="sm"
          className="min-h-11 w-full min-w-11 touch-manipulation sm:w-auto"
          onClick={onBack}
        >
          Back
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="min-h-11 w-full min-w-11 touch-manipulation sm:w-auto"
          onClick={onSkip}
        >
          Skip
        </Button>
      </DialogFooter>
    </OnboardingStepShell>
  );
}

export { QuestionnaireNextStepsCard };
