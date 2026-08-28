import { useRef } from "react";
import { GetStartedModal } from "@/components/onboarding/GetStartedModal";
import { OnboardingErrorBoundary } from "@/components/onboarding/OnboardingErrorBoundary";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";
import { useCreateWorkflowMutation } from "../workflows/hooks/useCreateWorkflowMutation";
import { Button } from "@/components/ui/button";
import { ReloadIcon } from "@radix-ui/react-icons";
import { defaultWorkflowRequest } from "../workflows/defaultWorkflowRequest";
import { WorkingExampleInspector } from "@/components/onboarding/WorkingExampleInspector";
import { OnboardingProgressCard } from "@/components/onboarding/OnboardingProgressCard";
import { useOnboardingProgress } from "./useOnboardingProgress";
import { onboardingExampleRequest } from "./onboardingExample";

function DiscoverPage() {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;
  const createWorkflowMutation = useCreateWorkflowMutation();
  const createInFlight = useRef(false);
  const {
    progress,
    isPending: onboardingProgressPending,
    dismiss,
    restore,
  } = useOnboardingProgress();

  const createWorkflow = (
    request: Parameters<typeof createWorkflowMutation.mutate>[0],
  ) => {
    if (createInFlight.current || createWorkflowMutation.isPending) return;
    createInFlight.current = true;
    createWorkflowMutation.mutate(request, {
      onSettled: () => {
        createInFlight.current = false;
      },
    });
  };

  const handleExampleCopy = () => {
    createWorkflow({
      ...onboardingExampleRequest,
      _via: "onboarding_example",
    });
  };

  const onboarding = useOnboardingStateOptional();

  return (
    <div className="space-y-10">
      <h1 className="sr-only">Create an agent</h1>
      <div className="space-y-3">
        <PromptBox enableCopilotHandoff={enableCopilotHandoff} />
        <div className="flex justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="h-11 touch-manipulation text-muted-foreground hover:text-foreground"
            disabled={createWorkflowMutation.isPending}
            onClick={() =>
              createWorkflow({
                ...defaultWorkflowRequest,
                _via: "blank",
              })
            }
          >
            {createWorkflowMutation.isPending && (
              <ReloadIcon
                aria-hidden="true"
                className="mr-2 h-3 w-3 motion-safe:animate-spin motion-reduce:animate-none"
              />
            )}
            Skip — start with blank canvas →
          </Button>
        </div>
      </div>
      {progress?.state === "active" && (
        <WorkingExampleInspector
          isPending={createWorkflowMutation.isPending}
          onMakeCopy={handleExampleCopy}
        />
      )}
      {progress?.state === "active" &&
      (progress.completed_count === 0 || progress.completed_count === 1) &&
      progress.next_action_key !== null ? (
        <OnboardingProgressCard
          state="active"
          completedCount={progress.completed_count}
          firstMilestoneComplete={progress.items.some(
            (item) =>
              item.key === "first_agent_created" && item.completed_at !== null,
          )}
          nextActionKey={progress.next_action_key}
          isPending={onboardingProgressPending}
          onDismiss={dismiss}
        />
      ) : progress?.state === "dismissed" ? (
        <OnboardingProgressCard
          state="dismissed"
          isPending={onboardingProgressPending}
          onRestore={restore}
        />
      ) : null}
      <WorkflowTemplates />
      {onboarding ? (
        <OnboardingErrorBoundary
          onError={() => OnboardingTelemetry.modalRenderError("discover")}
        >
          <GetStartedModal />
        </OnboardingErrorBoundary>
      ) : null}
    </div>
  );
}

export { DiscoverPage };
