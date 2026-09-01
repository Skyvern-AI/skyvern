import { useRef } from "react";
import { GetStartedModal } from "@/components/onboarding/GetStartedModal";
import { OnboardingErrorBoundary } from "@/components/onboarding/OnboardingErrorBoundary";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  PromptBox,
  type ExamplePromptKey,
  type PromptBoxHandle,
} from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";
import { useCreateWorkflowMutation } from "../workflows/hooks/useCreateWorkflowMutation";
import { Button } from "@/components/ui/button";
import { ReloadIcon } from "@radix-ui/react-icons";
import { defaultWorkflowRequest } from "../workflows/defaultWorkflowRequest";
import { WorkingExampleInspector } from "@/components/onboarding/WorkingExampleInspector";
import { OnboardingProgressBand } from "@/components/onboarding/OnboardingProgressBand";
import { useOnboardingProgress } from "./useOnboardingProgress";
import { onboardingExampleRequest } from "./onboardingExample";

function getIntentExampleKey(
  intent: string | null | undefined,
): ExamplePromptKey {
  switch (intent) {
    case "fill_forms":
      return "contact_us_forms";
    case "extract_data":
      return "hackernews";
    case "monitor_website":
      return "AAPLStockPrice";
    default:
      return "finditparts";
  }
}

function DiscoverPage() {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;
  const createWorkflowMutation = useCreateWorkflowMutation();
  const createInFlight = useRef(false);
  const promptBoxRef = useRef<PromptBoxHandle>(null);
  const onboarding = useOnboardingStateOptional();
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

  const handleDescribeAgent = () => {
    promptBoxRef.current?.focusAndPrefillExample(
      getIntentExampleKey(onboarding?.state?.user_intent),
    );
  };

  return (
    <div className="space-y-10">
      <h1 className="sr-only">Create an agent</h1>
      <div className="space-y-3">
        <PromptBox
          ref={promptBoxRef}
          enableCopilotHandoff={enableCopilotHandoff}
        />
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
      <OnboardingProgressBand
        progress={progress}
        isPending={onboardingProgressPending}
        onDismiss={dismiss}
        onRestore={restore}
        onDescribeAgent={handleDescribeAgent}
      >
        {progress?.state === "active" ? (
          <WorkingExampleInspector
            isPending={
              createWorkflowMutation.isPending || onboardingProgressPending
            }
            onMakeCopy={handleExampleCopy}
          />
        ) : null}
      </OnboardingProgressBand>
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
