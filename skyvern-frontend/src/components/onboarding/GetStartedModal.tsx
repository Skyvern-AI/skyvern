import { useEffect, useRef, useState } from "react";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useOnboardingState } from "@/store/onboarding/useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { ClickIcon } from "@/components/icons/ClickIcon";
import { ExtractIcon } from "@/components/icons/ExtractIcon";
import { SearchIcon } from "@/components/icons/SearchIcon";
import { CompassIcon } from "@/components/icons/CompassIcon";
import { cn } from "@/util/utils";
import { useGlobalWorkflowsQuery } from "@/routes/workflows/hooks/useGlobalWorkflowsQuery";
import { useCreateWorkflowMutation } from "@/routes/workflows/hooks/useCreateWorkflowMutation";
import { convert } from "@/routes/workflows/editor/workflowEditorUtils";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import {
  getTemplatesForIntent,
  getTemplateIcon,
  getSetupTime,
} from "./templateUtils";
import { CopilotCTAStep } from "./CopilotCTAStep";
import {
  type ABVariant,
  DEFAULT_VARIANT,
  VARIANTS,
  isABVariant,
  EXPERIMENT,
} from "@/util/onboarding/experimentConfig";

const SURFACE = "dashboard" as const;

type Step = "intent" | "templates";

type IntentOption = {
  id: string;
  label: string;
  description: string;
  icon: React.FC<{ className?: string }>;
};

const intentOptions: IntentOption[] = [
  {
    id: "fill_forms",
    label: "Fill out forms",
    description: "Automate form submissions across websites",
    icon: ClickIcon,
  },
  {
    id: "extract_data",
    label: "Extract data from websites",
    description: "Scrape and collect data at scale",
    icon: ExtractIcon,
  },
  {
    id: "monitor_website",
    label: "Monitor a website for changes",
    description: "Track updates and get notified",
    icon: SearchIcon,
  },
  {
    id: "something_else",
    label: "Something else",
    description: "Other browser automation tasks",
    icon: CompassIcon,
  },
];

type Props = {
  hasWorkflows: boolean;
  isLoading: boolean;
};

function resolveVariant(
  stateVariant: string | null | undefined,
  flagVariant: string | boolean | undefined,
): ABVariant {
  if (isABVariant(stateVariant)) {
    return stateVariant;
  }
  return flagVariant === VARIANTS.COPILOT_FIRST
    ? VARIANTS.COPILOT_FIRST
    : DEFAULT_VARIANT;
}

function GetStartedModal({ hasWorkflows, isLoading }: Readonly<Props>) {
  const { state, updateState } = useOnboardingState();
  const flagVariant = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const [step, setStep] = useState<Step>("intent");
  const [selectedIntent, setSelectedIntent] = useState<string | null>(null);
  const [copilotStepBusy, setCopilotStepBusy] = useState(false);
  const openedRef = useRef(false);
  const variantAssignedRef = useRef(false);

  const variant = resolveVariant(state?.ab_variant, flagVariant);

  const { data: globalTemplates = [], isLoading: templatesLoading } =
    useGlobalWorkflowsQuery();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const onboardingEnabled = isABVariant(flagVariant);

  const isOpen =
    onboardingEnabled &&
    !isLoading &&
    !hasWorkflows &&
    state !== null &&
    state.modal_dismissed_at === null &&
    state.first_save_at === null;

  useEffect(() => {
    if (state?.user_intent && state.modal_dismissed_at === null) {
      setSelectedIntent(state.user_intent);
      setStep("templates");
    }
  }, [state?.user_intent, state?.modal_dismissed_at]);

  useEffect(() => {
    if (isOpen && !openedRef.current) {
      openedRef.current = true;
      OnboardingTelemetry.registerVariant(variant);
      OnboardingTelemetry.flowStarted(SURFACE);
      OnboardingTelemetry.modalOpened(SURFACE);
    }
  }, [isOpen, variant]);

  useEffect(() => {
    if (!state || state.ab_variant !== null || variantAssignedRef.current)
      return;
    // only persist a variant once the flag resolves to a real arm; an unloaded/disabled flag (pre-load, 0% rollout, rollback) must not bias the split
    if (!isABVariant(flagVariant)) return;
    variantAssignedRef.current = true;
    updateState({ ab_variant: variant });
    OnboardingTelemetry.abVariantAssigned(SURFACE, variant);
  }, [state, variant, flagVariant, updateState]);

  function handleSelectIntent(intentId: string) {
    setSelectedIntent(intentId);
  }

  function handleContinue() {
    if (!selectedIntent) return;
    updateState({ user_intent: selectedIntent });
    setStep("templates");
  }

  function handleBack() {
    setStep("intent");
  }

  function handleSkip() {
    if (createWorkflowMutation.isPending || copilotStepBusy) return;
    OnboardingTelemetry.modalSkipped(SURFACE);
    updateState({ modal_dismissed_at: new Date().toISOString() });
  }

  function handleTemplateSelect(template: WorkflowApiResponse) {
    if (createWorkflowMutation.isPending) return;
    OnboardingTelemetry.modalTemplateSelected(
      SURFACE,
      template.workflow_permanent_id,
      selectedIntent!,
    );
    const cloned = convert({
      ...template,
      title: `${template.title} (copy)`,
    });
    // Completion telemetry fires from useCreateWorkflowMutation (it owns the
    // navigation that unmounts this modal); modal re-display is gated by
    // first_save_at + hasWorkflows, so no dismiss write is needed here.
    createWorkflowMutation.mutate({ ...cloned, _via: "onboarding_template" });
  }

  const filteredTemplates =
    selectedIntent && globalTemplates.length > 0
      ? getTemplatesForIntent(globalTemplates, selectedIntent)
      : [];

  return (
    <Dialog open={isOpen} onOpenChange={() => handleSkip()}>
      <DialogContent
        className="max-w-xl"
        onPointerDownOutside={(e) => e.preventDefault()}
      >
        {step === "intent" ? (
          <>
            <DialogHeader>
              <DialogTitle className="text-xl">
                What do you want to automate?
              </DialogTitle>
              <DialogDescription>
                Pick the option that best describes your goal. You can always
                change this later.
              </DialogDescription>
            </DialogHeader>
            <div className="grid grid-cols-2 gap-3 py-2">
              {intentOptions.map((option) => {
                const Icon = option.icon;
                const isSelected = selectedIntent === option.id;
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => handleSelectIntent(option.id)}
                    className={cn(
                      "flex flex-col items-start gap-2 rounded-lg border p-4 text-left transition-colors hover:bg-muted/50",
                      isSelected
                        ? "border-primary bg-primary/5"
                        : "border-border",
                    )}
                  >
                    <Icon className="h-6 w-6 text-primary" />
                    <div>
                      <p className="text-sm font-medium">{option.label}</p>
                      <p className="text-xs text-muted-foreground">
                        {option.description}
                      </p>
                    </div>
                  </button>
                );
              })}
            </div>
            <DialogFooter className="gap-2 sm:gap-0">
              <Button variant="ghost" size="sm" onClick={handleSkip}>
                Skip
              </Button>
              <Button
                size="sm"
                disabled={!selectedIntent}
                onClick={handleContinue}
              >
                Continue
              </Button>
            </DialogFooter>
          </>
        ) : variant === VARIANTS.COPILOT_FIRST ? (
          <CopilotCTAStep
            selectedIntent={selectedIntent!}
            onBack={handleBack}
            onSkip={handleSkip}
            onDismiss={() =>
              updateState({ modal_dismissed_at: new Date().toISOString() })
            }
            onBusyChange={setCopilotStepBusy}
          />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle className="text-xl">
                Pick a template to start
              </DialogTitle>
              <DialogDescription>
                Choose a pre-built workflow and customize it in the editor.
              </DialogDescription>
            </DialogHeader>
            <div className="grid grid-cols-2 gap-3 py-2">
              {templatesLoading ? (
                Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-28 rounded-lg" />
                ))
              ) : filteredTemplates.length > 0 ? (
                filteredTemplates.map((template) => {
                  const Icon = getTemplateIcon(template);
                  return (
                    <button
                      key={template.workflow_permanent_id}
                      type="button"
                      disabled={createWorkflowMutation.isPending}
                      onClick={() => handleTemplateSelect(template)}
                      className={cn(
                        "flex flex-col items-start gap-2 rounded-lg border border-border p-4 text-left transition-colors",
                        "hover:border-primary hover:bg-primary/5",
                        createWorkflowMutation.isPending &&
                          "pointer-events-none opacity-50",
                      )}
                    >
                      <Icon className="h-6 w-6 text-primary" />
                      <div className="min-w-0 self-stretch">
                        <p className="truncate text-sm font-medium">
                          {template.title}
                        </p>
                        {template.description && (
                          <p className="line-clamp-2 text-xs text-muted-foreground">
                            {template.description}
                          </p>
                        )}
                      </div>
                      <span className="text-xs text-muted-foreground">
                        ~{getSetupTime(template)} setup
                      </span>
                    </button>
                  );
                })
              ) : (
                <p className="col-span-2 py-8 text-center text-sm text-muted-foreground">
                  No templates available yet.
                </p>
              )}
            </div>
            <DialogFooter className="gap-2 sm:gap-0">
              <Button
                variant="ghost"
                size="sm"
                onClick={handleBack}
                disabled={createWorkflowMutation.isPending}
              >
                Back
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleSkip}
                disabled={createWorkflowMutation.isPending}
              >
                Skip
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export { GetStartedModal };
