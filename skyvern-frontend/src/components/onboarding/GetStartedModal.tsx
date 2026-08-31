import { ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { useAuth, useUser } from "@clerk/clerk-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useOnboardingState } from "@/store/onboarding/useOnboardingState";
import {
  isQuestionnaireUserIntentV1,
  type QuestionnaireAnswersV1,
  type QuestionnairePatchV1,
  type QuestionnaireStateV1,
} from "@/store/onboarding/types";
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
import { QuestionnaireDetailsStep } from "./QuestionnaireDetailsStep";
import { QuestionnaireNextStepsCard } from "./QuestionnaireNextStepsCard";
import { OnboardingOptionRow } from "./OnboardingOptionRow";
import { OnboardingStepProgress } from "./OnboardingStepProgress";
import { OnboardingStepShell } from "./OnboardingStepShell";
import {
  type ABVariant,
  DEFAULT_VARIANT,
  VARIANTS,
  isABVariant,
  EXPERIMENT,
} from "@/util/onboarding/experimentConfig";

const SURFACE = "discover" as const;

export const DECIDING_PLACEHOLDER_DELAY_MS = 500;

// Mirrors the backend questionnaire signup cutoff: reservation can only answer
// ineligible for accounts created before it, so those users skip the reserve
// call entirely. The server stays authoritative for everyone else.
export const QUESTIONNAIRE_SIGNUP_CUTOFF_MS = Date.UTC(2026, 7, 27);

type EditorStep = "intent" | "templates";
type DiscoverOnboardingOwner =
  | { kind: "deciding" }
  | { kind: "questionnaire"; step: "intent" }
  | { kind: "questionnaire"; step: "details" }
  | {
      kind: "questionnaire";
      step: "recommendations";
      answers: QuestionnaireAnswersV1;
    }
  | { kind: "editor" }
  | { kind: "closed" };

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

function answersFromQuestionnaire(
  questionnaire: QuestionnaireStateV1 | null,
): QuestionnaireAnswersV1 | null {
  if (!questionnaire) return null;
  const { role, company_context, scale_intent, referral_source } =
    questionnaire;
  return role && company_context && scale_intent && referral_source
    ? { role, company_context, scale_intent, referral_source }
    : null;
}

function questionnaireTerminalOwner(
  questionnaire: QuestionnaireStateV1,
): DiscoverOnboardingOwner {
  const answers =
    questionnaire.status === "completed"
      ? answersFromQuestionnaire(questionnaire)
      : null;
  return answers
    ? { kind: "questionnaire", step: "recommendations", answers }
    : { kind: "closed" };
}

function GetStartedModalForUser() {
  const { state, isLoading, isNewUser, updateState, updateStateConfirmed } =
    useOnboardingState();
  const { user, isLoaded: userLoaded } = useUser();
  const flagVariant = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const [owner, setOwner] = useState<DiscoverOnboardingOwner>({
    kind: "deciding",
  });
  const questionnaireOwner = owner.kind === "questionnaire";
  const [step, setStep] = useState<EditorStep>("intent");
  const [selectedIntent, setSelectedIntent] = useState<string | null>(null);
  const [questionnaire, setQuestionnaire] =
    useState<QuestionnaireStateV1 | null>(null);
  const [intentPending, setIntentPending] = useState(false);
  const [decidingPlaceholderVisible, setDecidingPlaceholderVisible] =
    useState(false);
  const [questionnairePending, setQuestionnairePending] = useState(false);
  const [intentError, setIntentError] = useState<string | null>(null);
  const [copilotStepBusy, setCopilotStepBusy] = useState(false);
  const openedRef = useRef(false);
  const variantAssignedRef = useRef(false);
  const confirmedEventsRef = useRef(new Set<string>());
  const openVisitRef = useRef(0);
  const isOpenRef = useRef(false);
  const reservationStartedRef = useRef(false);
  const reservationEditorStateRef = useRef<
    readonly [string | null, string | null, string | null] | null
  >(null);
  const ownerGenerationRef = useRef(0);
  const mountedRef = useRef(true);
  const latestStateRef = useRef(state);
  const questionnaireObservedDuringReservationRef = useRef(false);
  latestStateRef.current = state;
  const initialSkipMutationIdRef = useRef(crypto.randomUUID());

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const variant = resolveVariant(state?.ab_variant, flagVariant);

  const { data: globalTemplates = [], isLoading: templatesLoading } =
    useGlobalWorkflowsQuery();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const onboardingEnabled = isABVariant(flagVariant);
  const editorOpen =
    owner.kind === "editor" &&
    onboardingEnabled &&
    state !== null &&
    state.modal_dismissed_at === null &&
    state.first_save_at === null &&
    state.questionnaire_prompted_at == null &&
    state.questionnaire == null &&
    (isOpenRef.current || isNewUser);
  const reservationCandidate =
    owner.kind === "deciding" &&
    !isLoading &&
    state !== null &&
    state.questionnaire_prompted_at == null &&
    state.questionnaire == null;
  const questionnaireLocallyIneligible =
    userLoaded &&
    (user?.createdAt == null ||
      user.createdAt.getTime() < QUESTIONNAIRE_SIGNUP_CUTOFF_MS);
  const decidingOpen = reservationCandidate;
  const ownedOpen = questionnaireOwner || editorOpen;
  const isOpen = decidingOpen || ownedOpen;
  // The deciding dialog must block interaction but stay invisible at first, so
  // a reservation that resolves to "nothing to show" never flashes the
  // placeholder; it is revealed only if the check outlasts the grace delay.
  const decidingHidden =
    owner.kind === "deciding" && !decidingPlaceholderVisible;
  useEffect(() => {
    if (!decidingOpen) return;
    const timer = window.setTimeout(
      () => setDecidingPlaceholderVisible(true),
      DECIDING_PLACEHOLDER_DELAY_MS,
    );
    return () => window.clearTimeout(timer);
  }, [decidingOpen]);
  useLayoutEffect(() => {
    if (ownedOpen && !isOpenRef.current) openVisitRef.current += 1;
    isOpenRef.current = ownedOpen;
  }, [ownedOpen]);
  useLayoutEffect(() => {
    if (!state) return;
    const editorState = [
      state.modal_dismissed_at,
      state.first_save_at,
      state.user_intent,
    ] as const;
    if (
      owner.kind === "deciding" &&
      reservationStartedRef.current &&
      (state.questionnaire_prompted_at || state.questionnaire)
    ) {
      questionnaireObservedDuringReservationRef.current = true;
    }
    const reservationEditorState = reservationEditorStateRef.current;
    if (
      owner.kind === "deciding" &&
      reservationStartedRef.current &&
      reservationEditorState &&
      (editorState[0] !== reservationEditorState[0] ||
        editorState[1] !== reservationEditorState[1])
    ) {
      ownerGenerationRef.current += 1;
      setOwner({ kind: "closed" });
      return;
    }
    if (owner.kind !== "deciding" || isLoading) {
      return;
    }
    if (reservationStartedRef.current) {
      return;
    }
    if (!reservationCandidate) {
      setOwner({ kind: "closed" });
      return;
    }
    if (!userLoaded) {
      return;
    }
    if (questionnaireLocallyIneligible) {
      if (state.modal_dismissed_at !== null || state.first_save_at !== null) {
        setOwner({ kind: "closed" });
        return;
      }
      if (state.user_intent) {
        setSelectedIntent(state.user_intent);
        setStep("templates");
      }
      setOwner({ kind: "editor" });
      return;
    }
    reservationStartedRef.current = true;
    reservationEditorStateRef.current = editorState;
    const generation = ++ownerGenerationRef.current;
    void updateStateConfirmed({
      questionnaire_prompt: { version: 1, action: "reserve" },
    })
      .then((response) => {
        if (!mountedRef.current || generation !== ownerGenerationRef.current)
          return;
        if ("code" in response) {
          setOwner({ kind: "closed" });
          return;
        }
        const result = response.questionnaire_prompt_result;
        if (
          result?.status === "reserved" &&
          typeof result.prompted_at === "string" &&
          result.prompted_at.length > 0
        ) {
          OnboardingTelemetry.questionnaireShown({
            primaryIntent: null,
            promptReason: "initial",
          });
          setOwner({ kind: "questionnaire", step: "intent" });
        } else if (
          result?.status === "flag_disabled" ||
          result?.status === "ineligible"
        ) {
          const latestState = latestStateRef.current;
          if (
            questionnaireObservedDuringReservationRef.current ||
            !latestState ||
            latestState.questionnaire_prompted_at ||
            latestState.questionnaire ||
            latestState.modal_dismissed_at !== null ||
            latestState.first_save_at !== null
          ) {
            setOwner({ kind: "closed" });
            return;
          }
          const fallbackIntent =
            latestState.user_intent ??
            reservationEditorStateRef.current?.[2] ??
            null;
          if (fallbackIntent) {
            setSelectedIntent(fallbackIntent);
            setStep("templates");
          }
          setOwner({ kind: "editor" });
        } else {
          setOwner({ kind: "closed" });
        }
      })
      .catch(() => {
        if (mountedRef.current && generation === ownerGenerationRef.current) {
          setOwner({ kind: "closed" });
        }
      });
  }, [
    isLoading,
    owner.kind,
    questionnaireLocallyIneligible,
    reservationCandidate,
    state,
    updateStateConfirmed,
    userLoaded,
  ]);

  useEffect(() => {
    if (isOpen && owner.kind !== "deciding" && !openedRef.current) {
      openedRef.current = true;
      if (owner.kind === "editor") {
        OnboardingTelemetry.registerVariant(variant);
      }
      OnboardingTelemetry.flowStarted(SURFACE);
      OnboardingTelemetry.modalOpened(SURFACE);
    }
  }, [isOpen, owner.kind, variant]);

  useEffect(() => {
    if (
      owner.kind !== "editor" ||
      !state ||
      state.ab_variant !== null ||
      variantAssignedRef.current
    )
      return;
    // only persist a variant once the flag resolves to a real arm; an unloaded/disabled flag (pre-load, 0% rollout, rollback) must not bias the split
    if (!isABVariant(flagVariant)) return;
    variantAssignedRef.current = true;
    updateState({ ab_variant: variant });
    OnboardingTelemetry.abVariantAssigned(SURFACE, variant);
  }, [owner.kind, state, variant, flagVariant, updateState]);

  function handleSelectIntent(intentId: string) {
    if (intentPending) return;
    setSelectedIntent(intentId);
  }

  async function handleContinue() {
    if (!selectedIntent || intentPending) {
      return;
    }
    setIntentError(null);
    if (owner.kind === "editor") {
      updateState({ user_intent: selectedIntent });
      setStep("templates");
      return;
    }
    if (
      owner.kind !== "questionnaire" ||
      !isQuestionnaireUserIntentV1(selectedIntent)
    ) {
      setIntentError("We couldn't save your choice. Try again.");
      return;
    }

    const visitId = openVisitRef.current;
    setIntentPending(true);
    try {
      const response = await updateStateConfirmed({
        user_intent: selectedIntent,
      });
      if ("code" in response) {
        throw new Error(`Intent confirmation failed: ${response.code}`);
      }
      if (!isOpenRef.current || visitId !== openVisitRef.current) {
        return;
      }
      const nextState = response.onboarding_state;
      if (!isQuestionnaireUserIntentV1(nextState.user_intent)) {
        setOwner({ kind: "closed" });
        return;
      }
      const confirmedQuestionnaire = nextState.questionnaire ?? null;
      if (confirmedQuestionnaire !== null) {
        if (questionnaire?.response_id !== confirmedQuestionnaire.response_id) {
          setOwner({ kind: "closed" });
          return;
        }
        setQuestionnaire(confirmedQuestionnaire);
        setOwner(questionnaireTerminalOwner(confirmedQuestionnaire));
        return;
      }
      setOwner({ kind: "questionnaire", step: "details" });
    } catch {
      if (isOpenRef.current && visitId === openVisitRef.current) {
        setIntentError("We couldn't save your choice. Try again.");
      }
    } finally {
      setIntentPending(false);
    }
  }

  function handleBack() {
    if (owner.kind === "questionnaire") {
      setOwner(
        owner.step === "recommendations"
          ? { kind: "questionnaire", step: "details" }
          : { kind: "questionnaire", step: "intent" },
      );
      return;
    }
    setStep("intent");
  }

  function handleSkip() {
    if (owner.kind === "deciding" || intentPending || questionnairePending)
      return;
    if (owner.kind === "questionnaire") {
      if (questionnaire !== null) {
        OnboardingTelemetry.modalSkipped(SURFACE);
        updateState({ modal_dismissed_at: new Date().toISOString() });
        setOwner({ kind: "closed" });
        return;
      }
      setIntentError(null);
      void handleQuestionnaireAction({
        version: 1,
        mutation_id: initialSkipMutationIdRef.current,
        expected_revision: 0,
        action: "skip",
      }).catch(() =>
        setIntentError("We couldn't save your choice. Try again."),
      );
      return;
    }
    if (createWorkflowMutation.isPending || copilotStepBusy) {
      return;
    }
    OnboardingTelemetry.modalSkipped(SURFACE);
    updateState({ modal_dismissed_at: new Date().toISOString() });
  }

  function emitQuestionnaireEvent(
    patch: QuestionnairePatchV1,
    previousQuestionnaire: QuestionnaireStateV1 | null,
    nextQuestionnaire: QuestionnaireStateV1,
    primaryIntent: string | null,
  ) {
    if (nextQuestionnaire.last_mutation_id !== patch.mutation_id) return;
    const eventName = `onboarding_questionnaire_${
      patch.action === "skip"
        ? "skipped"
        : patch.action === "complete"
          ? "completed"
          : "updated"
    }`;
    const eventKey = `${nextQuestionnaire.response_id}:${nextQuestionnaire.revision}:${eventName}`;
    if (confirmedEventsRef.current.has(eventKey)) return;
    if (patch.action === "skip") {
      if (nextQuestionnaire.status !== "skipped") return;
      confirmedEventsRef.current.add(eventKey);
      OnboardingTelemetry.questionnaireSkipped({
        responseId: nextQuestionnaire.response_id,
        revision: nextQuestionnaire.revision,
        disposition: "skip",
        statusAfter: "skipped",
      });
      return;
    }
    if (!isQuestionnaireUserIntentV1(primaryIntent)) return;
    const answers = answersFromQuestionnaire(nextQuestionnaire);
    if (nextQuestionnaire.status !== "completed" || !answers) return;

    if (patch.action === "complete") {
      confirmedEventsRef.current.add(eventKey);
      OnboardingTelemetry.questionnaireCompleted({
        responseId: nextQuestionnaire.response_id,
        revision: nextQuestionnaire.revision,
        primaryIntent,
        answers,
      });
      return;
    }
    if (!previousQuestionnaire) return;
    confirmedEventsRef.current.add(eventKey);
    OnboardingTelemetry.questionnaireUpdated({
      responseId: nextQuestionnaire.response_id,
      revision: nextQuestionnaire.revision,
      primaryIntent,
      previousStatus: previousQuestionnaire.status,
      answers,
    });
  }

  async function handleQuestionnaireAction(patch: QuestionnairePatchV1) {
    if (!questionnaireOwner) {
      throw new Error("Questionnaire visit is closed");
    }
    const visitId = openVisitRef.current;
    const previousQuestionnaire = questionnaire;
    setIntentError(null);
    setQuestionnairePending(true);
    try {
      const response = await updateStateConfirmed({ questionnaire: patch });
      if ("code" in response) {
        throw new Error(`Questionnaire confirmation failed: ${response.code}`);
      }
      if (!isOpenRef.current || visitId !== openVisitRef.current) {
        return;
      }
      const nextQuestionnaire = response.onboarding_state.questionnaire;
      if (!nextQuestionnaire) {
        throw new Error("Questionnaire confirmation was missing");
      }
      emitQuestionnaireEvent(
        patch,
        previousQuestionnaire,
        nextQuestionnaire,
        response.onboarding_state.user_intent,
      );
      setQuestionnaire(nextQuestionnaire);
      setOwner(
        patch.action === "skip"
          ? { kind: "closed" }
          : questionnaireTerminalOwner(nextQuestionnaire),
      );
    } finally {
      setQuestionnairePending(false);
    }
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
    // navigation that unmounts this modal); first_save_at prevents re-display,
    // so no dismiss write is needed here.
    createWorkflowMutation.mutate({ ...cloned, _via: "onboarding_template" });
  }

  const filteredTemplates =
    selectedIntent && globalTemplates.length > 0
      ? getTemplatesForIntent(globalTemplates, selectedIntent)
      : [];
  const stepCount = questionnaireOwner ? 3 : 2;
  const view =
    owner.kind === "questionnaire"
      ? owner.step
      : owner.kind === "editor" && editorOpen
        ? step
        : null;

  return (
    <Dialog open={isOpen} onOpenChange={() => handleSkip()}>
      <DialogContent
        className={cn(
          "z-[2147480003] grid max-h-[calc(100dvh-2rem)] max-w-xl grid-rows-[minmax(0,1fr)] overflow-hidden overscroll-contain [&>button]:right-2 [&>button]:top-2 [&>button]:inline-flex [&>button]:size-11 [&>button]:touch-manipulation [&>button]:items-center [&>button]:justify-center [&>button]:motion-reduce:transition-none",
          decidingHidden && "opacity-0",
        )}
        overlayClassName={decidingHidden ? "opacity-0" : undefined}
        onPointerDownOutside={(e) => e.preventDefault()}
      >
        {view === null ? (
          owner.kind === "deciding" && decidingOpen ? (
            <DialogHeader className="pr-12">
              <DialogTitle className="text-xl font-semibold">
                Getting started
              </DialogTitle>
              <DialogDescription>
                Checking your onboarding setup.
              </DialogDescription>
            </DialogHeader>
          ) : null
        ) : view === "intent" ? (
          <OnboardingStepShell stepIndex={1} stepCount={stepCount}>
            <DialogHeader className="pr-12">
              <DialogTitle className="text-xl font-semibold">
                What do you want to automate?
              </DialogTitle>
              <DialogDescription>
                Pick the option that best describes your goal. You can always
                change this later.
              </DialogDescription>
            </DialogHeader>
            <div className="flex min-h-0 flex-col gap-2 overflow-y-auto overscroll-contain pr-1">
              {intentOptions.map((option) => (
                <OnboardingOptionRow
                  key={option.id}
                  icon={option.icon}
                  label={option.label}
                  description={option.description}
                  selected={selectedIntent === option.id}
                  disabled={intentPending}
                  onClick={() => handleSelectIntent(option.id)}
                />
              ))}
            </div>
            {intentError ? (
              <Alert role="status" aria-live="polite" variant="destructive">
                <AlertDescription>{intentError}</AlertDescription>
              </Alert>
            ) : null}
            <DialogFooter className="sticky bottom-0 z-10 -mx-6 -mb-6 flex-col gap-2 border-t border-border bg-background px-6 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4 sm:flex-row sm:justify-between sm:gap-0 sm:space-x-0">
              <Button
                variant="ghost"
                size="sm"
                className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
                onClick={handleSkip}
                disabled={intentPending}
              >
                Skip
              </Button>
              <Button
                size="sm"
                className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
                disabled={!selectedIntent || intentPending}
                onClick={() => void handleContinue()}
              >
                {intentPending ? (
                  <ReloadIcon
                    aria-hidden
                    className="mr-2 size-4 animate-spin motion-reduce:animate-none"
                  />
                ) : null}
                Continue
              </Button>
            </DialogFooter>
          </OnboardingStepShell>
        ) : owner.kind === "questionnaire" && owner.step === "details" ? (
          <QuestionnaireDetailsStep
            completionAction={questionnaire ? "update" : "complete"}
            expectedRevision={questionnaire?.revision ?? 0}
            initialAnswers={answersFromQuestionnaire(questionnaire)}
            externalError={intentError}
            isPending={questionnairePending}
            onAction={handleQuestionnaireAction}
            onBack={handleBack}
          />
        ) : owner.kind === "questionnaire" &&
          owner.step === "recommendations" ? (
          <QuestionnaireNextStepsCard
            answers={owner.answers}
            onAction={() => {
              updateState({ modal_dismissed_at: new Date().toISOString() });
              setOwner({ kind: "closed" });
            }}
            onBack={handleBack}
            onSkip={handleSkip}
          />
        ) : variant === VARIANTS.COPILOT_FIRST ? (
          <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-4">
            <OnboardingStepProgress
              stepIndex={stepCount}
              stepCount={stepCount}
              chip="final"
            />
            <div
              className={cn(
                "grid min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-4 [&>div:first-child]:pr-12 [&>div:nth-child(2)]:min-h-0 [&>div:nth-child(2)]:overflow-y-auto [&>div:nth-child(2)]:overscroll-contain [&>div:nth-child(2)_.animate-spin]:motion-reduce:animate-none",
                "[&>div:nth-child(2)>button]:min-h-[3.5rem] [&>div:nth-child(2)>button]:touch-manipulation [&>div:nth-child(2)>button]:flex-row [&>div:nth-child(2)>button]:flex-wrap [&>div:nth-child(2)>button]:items-center [&>div:nth-child(2)>button]:py-3 [&>div:nth-child(2)>button]:motion-reduce:transition-none [&>div:nth-child(2)>div]:h-[3.5rem] [&>div:nth-child(2)>p]:col-span-1 [&>div:nth-child(2)]:grid-cols-1",
                "[&>div:nth-child(2)>button>div]:flex-1 [&>div:nth-child(2)>button>div]:basis-[calc(100%-2rem)] [&>div:nth-child(2)>button>div]:sm:basis-0 [&>div:nth-child(2)>button>svg]:h-5 [&>div:nth-child(2)>button>svg]:w-5 [&>div:nth-child(2)>button>svg]:shrink-0",
                "[&>div:nth-child(2)>button>span]:basis-full [&>div:nth-child(2)>button>span]:pl-8 [&>div:nth-child(2)>button>span]:tabular-nums [&>div:nth-child(2)>button>span]:sm:ml-auto [&>div:nth-child(2)>button>span]:sm:basis-auto [&>div:nth-child(2)>button>span]:sm:pl-0 [&>div:nth-child(2)>button>span]:sm:text-right",
                "[&>div:last-child]:sticky [&>div:last-child]:bottom-0 [&>div:last-child]:z-10 [&>div:last-child]:-mx-6 [&>div:last-child]:-mb-6 [&>div:last-child]:border-t [&>div:last-child]:border-border [&>div:last-child]:bg-background [&>div:last-child]:px-6 [&>div:last-child]:pb-[max(1rem,env(safe-area-inset-bottom))] [&>div:last-child]:pt-4",
                "[&>div:last-child_button]:min-h-11 [&>div:last-child_button]:w-full [&>div:last-child_button]:min-w-11 [&>div:last-child_button]:touch-manipulation [&>div:last-child_button]:motion-reduce:transition-none [&>div:last-child_button]:sm:w-auto",
              )}
              data-testid="copilot-chrome"
            >
              <CopilotCTAStep
                selectedIntent={selectedIntent!}
                onBack={handleBack}
                onSkip={handleSkip}
                onDismiss={() =>
                  updateState({ modal_dismissed_at: new Date().toISOString() })
                }
                onBusyChange={setCopilotStepBusy}
              />
            </div>
          </div>
        ) : (
          <OnboardingStepShell
            stepIndex={stepCount}
            stepCount={stepCount}
            chip="final"
          >
            <DialogHeader className="pr-12">
              <DialogTitle className="text-xl font-semibold">
                Pick a template to start
              </DialogTitle>
              <DialogDescription>
                Choose a pre-built workflow and customize it in the editor.
              </DialogDescription>
            </DialogHeader>
            <div className="flex min-h-0 flex-col gap-2 overflow-y-auto overscroll-contain pr-1">
              {templatesLoading ? (
                Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-[3.5rem] rounded-lg" />
                ))
              ) : filteredTemplates.length > 0 ? (
                filteredTemplates.map((template) => (
                  <OnboardingOptionRow
                    key={template.workflow_permanent_id}
                    icon={getTemplateIcon(template)}
                    label={template.title}
                    description={template.description}
                    meta={`~${getSetupTime(template)} setup`}
                    disabled={createWorkflowMutation.isPending}
                    onClick={() => handleTemplateSelect(template)}
                  />
                ))
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No templates available yet.
                </p>
              )}
            </div>
            <DialogFooter className="sticky bottom-0 z-10 -mx-6 -mb-6 flex-col gap-2 border-t border-border bg-background px-6 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4 sm:flex-row sm:justify-between sm:gap-0 sm:space-x-0">
              <Button
                variant="ghost"
                size="sm"
                className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
                onClick={handleBack}
                disabled={createWorkflowMutation.isPending}
              >
                Back
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
                onClick={handleSkip}
                disabled={createWorkflowMutation.isPending}
              >
                Skip
              </Button>
            </DialogFooter>
          </OnboardingStepShell>
        )}
      </DialogContent>
    </Dialog>
  );
}

function GetStartedModal() {
  const { userId } = useAuth();
  return <GetStartedModalForUser key={userId ?? "signed-out"} />;
}

export { GetStartedModal };
