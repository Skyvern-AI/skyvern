import posthog, { type Properties } from "posthog-js";
import type {
  QuestionnaireAnswersV1,
  QuestionnaireStatusV1,
  QuestionnaireUserIntentV1,
} from "@/store/onboarding/types";

type Surface =
  | "dashboard"
  | "discover"
  | "editor"
  | "runs"
  | "settings"
  | "api_docs";

type TourLayer = 1 | 2;

type QuestionnaireShownInput = {
  primaryIntent: QuestionnaireUserIntentV1 | null;
  promptReason: "initial";
  organizationId: string | null;
};

type QuestionnaireResponseInput = {
  responseId: string;
  revision: number;
};

type QuestionnaireAnswerInput = QuestionnaireResponseInput & {
  primaryIntent: QuestionnaireUserIntentV1;
  answers: QuestionnaireAnswersV1;
};

type QuestionnaireSkippedInput = QuestionnaireResponseInput & {
  disposition: "skip";
  statusAfter: "skipped";
};

type QuestionnaireUpdatedInput = QuestionnaireAnswerInput & {
  previousStatus: QuestionnaireStatusV1;
};

const ONBOARDING_STEP_VERSION = "v1";

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

// The org group registers after GET /organizations/ resolves and can lose the race
// against a fresh signup's first onboarding event, so a caller-provided org wins.
function capture(event: string, properties: Properties): boolean {
  let organizationId = nonEmptyString(properties.organization_id);
  if (organizationId === undefined) {
    try {
      organizationId = nonEmptyString(posthog.getGroups().organization);
    } catch {
      organizationId = undefined;
    }
  }

  try {
    const stepName =
      typeof properties.step === "string" ? properties.step : event;
    const captureResult = posthog.capture(event, {
      ...properties,
      organization_id: organizationId,
      step_id: `${stepName}:${ONBOARDING_STEP_VERSION}`,
    });
    return captureResult !== undefined;
  } catch {
    // PostHog may be unavailable in tests or before init.
    return false;
  }
}

// -- Flow events --

function flowStarted(surface: Surface): void {
  capture("onboarding.flow_started", { surface });
}

function flowCompleted(surface: Surface): void {
  capture("onboarding.flow_completed", { surface });
}

function dropOff(surface: Surface, step: string): void {
  capture("onboarding.drop_off", { surface, step });
}

// -- Tour events --

function tourStarted(surface: Surface): void {
  capture("onboarding.tour_started", { surface });
}

function tourStepViewed(
  surface: Surface,
  stepName: string,
  stepIndex: number,
  layer: TourLayer,
): void {
  capture("onboarding.tour_step_viewed", {
    surface,
    step_name: stepName,
    step_index: stepIndex,
    layer,
  });
}

function tourCompleted(surface: Surface): void {
  capture("onboarding.tour_completed", { surface });
}

function tourSkipped(surface: Surface, atStep: string): void {
  capture("onboarding.tour_skipped", { surface, at_step: atStep });
}

function stepCompleted(surface: Surface, step: string): void {
  capture("onboarding.step_completed", { surface, step });
}

function tourDismissed(surface: Surface, lastStep: string): void {
  capture("onboarding.tour_dismissed", { surface, last_step: lastStep });
}

// -- Modal events --

function modalOpened(surface: Surface): void {
  capture("onboarding.modal_opened", { surface });
}

function modalTemplateSelected(
  surface: Surface,
  templateId: string,
  intent: string,
): void {
  capture("onboarding.modal_template_selected", {
    surface,
    template_id: templateId,
    intent,
  });
}

function modalCopilotClicked(
  surface: Surface,
  intent: string,
  promptText: string,
): void {
  capture("onboarding.modal_copilot_clicked", {
    surface,
    intent,
    // capture only length, never the raw prompt - it can contain customer URLs / PII
    prompt_length: promptText.length,
  });
}

function modalSkipped(surface: Surface): void {
  capture("onboarding.modal_skipped", { surface });
}

function questionnaireResponseProperties(input: QuestionnaireResponseInput) {
  return {
    questionnaire_version: 1,
    "$feature/onboarding_questionnaire_v1": true,
    surface: "get_started_modal",
    response_id: input.responseId,
    revision: input.revision,
  };
}

function questionnaireAnswerProperties(answers: QuestionnaireAnswersV1) {
  return {
    role: answers.role,
    company_context: answers.company_context,
    scale_intent: answers.scale_intent,
    referral_source: answers.referral_source,
  };
}

function questionnaireShown(input: QuestionnaireShownInput): boolean {
  return capture("onboarding_questionnaire_shown", {
    questionnaire_version: 1,
    "$feature/onboarding_questionnaire_v1": true,
    surface: "get_started_modal",
    primary_intent: input.primaryIntent,
    prompt_reason: input.promptReason,
    ...(input.organizationId ? { organization_id: input.organizationId } : {}),
  });
}

function questionnaireCompleted(input: QuestionnaireAnswerInput): void {
  capture("onboarding_questionnaire_completed", {
    ...questionnaireResponseProperties(input),
    primary_intent: input.primaryIntent,
    ...questionnaireAnswerProperties(input.answers),
  });
}

function questionnaireSkipped(input: QuestionnaireSkippedInput): void {
  capture("onboarding_questionnaire_skipped", {
    ...questionnaireResponseProperties(input),
    disposition: input.disposition,
    status_after: input.statusAfter,
  });
}

function questionnaireUpdated(input: QuestionnaireUpdatedInput): void {
  capture("onboarding_questionnaire_updated", {
    ...questionnaireResponseProperties(input),
    primary_intent: input.primaryIntent,
    previous_status: input.previousStatus,
    ...questionnaireAnswerProperties(input.answers),
  });
}

// -- Activation milestones --

function firstWorkflowCreated(surface: Surface): void {
  capture("onboarding.first_workflow_created", { surface });
}

function firstRunCompleted(surface: Surface): void {
  capture("onboarding.first_run_completed", { surface });
}

function firstApiCall(surface: Surface): void {
  capture("onboarding.first_api_call", { surface });
}

function firstScheduleCreated(surface: Surface): void {
  capture("onboarding.first_schedule_created", { surface });
}

// -- Empty state events --

function emptyStateViewed(surface: Surface): void {
  capture("onboarding.empty_state_viewed", { surface });
}

function emptyStateCTAClicked(surface: Surface, action: string): void {
  capture("onboarding.empty_state_cta_clicked", { surface, action });
}

// -- Experiment --

function abVariantAssigned(surface: Surface, variant: string): void {
  capture("onboarding.ab_variant_assigned", { surface, variant });
}

function registerVariant(variant: string): void {
  try {
    // Super property "variant" so every onboarding event can be split by arm (matches the dashboard breakdowns).
    posthog.register({ variant });
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

// -- Error events (rollback guardrails) --

function error(surface: Surface): void {
  capture("onboarding.error", { surface });
}

function modalRenderError(surface: Surface): void {
  capture("onboarding.modal_render_error", { surface });
}

function tourError(surface: Surface): void {
  capture("onboarding.tour_error", { surface });
}

// -- Contextual hint events (Layer 2) --

function hintShown(surface: Surface, hintId: string): void {
  capture("onboarding.hint_shown", { surface, hint_id: hintId, layer: 2 });
}

function hintDismissed(surface: Surface, hintId: string): void {
  capture("onboarding.hint_dismissed", { surface, hint_id: hintId, layer: 2 });
}

export const OnboardingTelemetry = {
  flowStarted,
  flowCompleted,
  dropOff,
  tourStarted,
  tourStepViewed,
  tourCompleted,
  tourSkipped,
  stepCompleted,
  tourDismissed,
  modalOpened,
  modalTemplateSelected,
  modalCopilotClicked,
  modalSkipped,
  questionnaireShown,
  questionnaireCompleted,
  questionnaireSkipped,
  questionnaireUpdated,
  emptyStateViewed,
  emptyStateCTAClicked,
  firstWorkflowCreated,
  firstRunCompleted,
  firstApiCall,
  firstScheduleCreated,
  abVariantAssigned,
  registerVariant,
  error,
  modalRenderError,
  tourError,
  hintShown,
  hintDismissed,
} as const;

export type { Surface, TourLayer };
