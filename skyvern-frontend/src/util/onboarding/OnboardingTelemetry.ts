import posthog from "posthog-js";

type Surface = "dashboard" | "editor" | "runs" | "settings" | "api_docs";

type TourLayer = 1 | 2;

function capture(event: string, properties: Record<string, unknown>): void {
  try {
    posthog.capture(event, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
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
