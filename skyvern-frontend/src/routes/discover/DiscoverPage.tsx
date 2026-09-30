import { useEffect, useRef } from "react";
import { HomeTelemetry } from "@/util/homeTelemetry";
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
import { useLocation, useSearchParams } from "react-router-dom";
import { FilePlusIcon, ReloadIcon } from "@radix-ui/react-icons";
import { defaultWorkflowRequest } from "../workflows/defaultWorkflowRequest";

function getIntentExampleKey(
  intent: string | null | undefined,
): ExamplePromptKey {
  switch (intent) {
    case "fill_forms":
      return "contact_us_forms";
    case "job_applications":
      return "job_application";
    case "extract_data":
      return "extractIntegrationsFromGong";
    case "monitor_website":
      return "AAPLStockPrice";
    default:
      return "finditparts";
  }
}

type Props = {
  /** The redesigned home screen; off renders the legacy discover page. */
  revamp?: boolean;
  /** Called when the user creates their first agent from the redesigned home. */
  onRevampComplete?: () => void;
};

function DiscoverPage({ revamp = false, onRevampComplete }: Props = {}) {
  const enableCopilotHandoff =
    useFeatureFlag("ENABLE_DISCOVER_COPILOT_HANDOFF") === true;
  const createWorkflowMutation = useCreateWorkflowMutation({
    onCreated: revamp ? onRevampComplete : undefined,
  });
  const createInFlight = useRef(false);
  const promptBoxRef = useRef<PromptBoxHandle>(null);
  const handledFocus = useRef(false);
  const onboarding = useOnboardingStateOptional();
  const exposureRecorded = useRef(false);

  useEffect(() => {
    if (exposureRecorded.current) return;
    exposureRecorded.current = true;
    HomeTelemetry.viewed(revamp ? "revamp" : "legacy");
  }, [revamp]);

  const createWorkflow = (
    request: Parameters<typeof createWorkflowMutation.mutate>[0],
  ) => {
    if (createInFlight.current || createWorkflowMutation.isPending) return;
    createInFlight.current = true;
    const attempt = HomeTelemetry.agentCreationSubmitted({
      source: "blank",
      handoff: false,
      variant: revamp ? "revamp" : "legacy",
    });
    HomeTelemetry.skipToBlankCanvasClicked(attempt);
    createWorkflowMutation.mutate(
      {
        ...request,
        _agentCreationAttempt: attempt,
      },
      {
        onSettled: () => {
          createInFlight.current = false;
        },
      },
    );
  };

  // `/discover?focus=prompt` is the sidebar card's first-agent link: focus + prefill once, then drop the param.
  const [searchParams, setSearchParams] = useSearchParams();
  const focusPrompt = searchParams.get("focus") === "prompt";
  const requestedExample = searchParams.get("example");
  // Free text arrives in router state rather than the URL, so it never lands in history or logs.
  const locationState: unknown = useLocation().state;
  const prefillPrompt =
    locationState &&
    typeof locationState === "object" &&
    "prefillPrompt" in locationState &&
    typeof locationState.prefillPrompt === "string"
      ? locationState.prefillPrompt
      : null;
  useEffect(() => {
    if (!focusPrompt) {
      handledFocus.current = false;
      return;
    }
    if (onboarding?.isLoading) return;
    if (!handledFocus.current) {
      const promptBox = promptBoxRef.current;
      if (!promptBox) return;
      handledFocus.current = true;
      if (prefillPrompt) {
        promptBox.focusAndPrefillPrompt(prefillPrompt);
      } else {
        promptBox.focusAndPrefillExample(
          requestedExample,
          getIntentExampleKey(onboarding?.state?.user_intent),
        );
      }
    }
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("focus");
        next.delete("example");
        return next;
      },
      { replace: true },
    );
  }, [
    focusPrompt,
    onboarding?.isLoading,
    onboarding?.state?.user_intent,
    prefillPrompt,
    requestedExample,
    setSearchParams,
  ]);

  if (revamp) {
    return (
      <div className="flex min-h-[calc(100vh-9rem)] flex-col justify-center">
        <h1 className="sr-only">Create an agent</h1>
        <PromptBox
          ref={promptBoxRef}
          enableCopilotHandoff={enableCopilotHandoff}
          minimal
          onAgentCreated={onRevampComplete}
        />
        <div className="mt-3 flex justify-center">
          <Button
            variant="ghost"
            size="sm"
            className="h-11 touch-manipulation text-muted-foreground hover:text-foreground"
            disabled={createWorkflowMutation.isPending}
            onClick={() => {
              createWorkflow({
                ...defaultWorkflowRequest,
                _via: "blank",
              });
            }}
          >
            {createWorkflowMutation.isPending && (
              <ReloadIcon
                aria-hidden="true"
                className="mr-2 h-3 w-3 motion-safe:animate-spin motion-reduce:animate-none"
              />
            )}
            Skip — start from a blank agent
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-10">
      <h1 className="sr-only">Create an agent</h1>
      <PromptBox
        ref={promptBoxRef}
        enableCopilotHandoff={enableCopilotHandoff}
        secondaryAction={
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-full touch-manipulation gap-2 border border-border/70 text-muted-foreground hover:text-foreground md:h-10 md:w-auto md:border-0"
            disabled={createWorkflowMutation.isPending}
            onClick={() => {
              createWorkflow({
                ...defaultWorkflowRequest,
                _via: "blank",
              });
            }}
          >
            {createWorkflowMutation.isPending ? (
              <ReloadIcon
                aria-hidden="true"
                className="h-3 w-3 motion-safe:animate-spin motion-reduce:animate-none"
              />
            ) : (
              <FilePlusIcon aria-hidden="true" className="size-4" />
            )}
            Start with a blank canvas
          </Button>
        }
      />
      <div className="mx-auto w-full max-w-[60rem] pb-8">
        <WorkflowTemplates />
      </div>
    </div>
  );
}

export { DiscoverPage };
