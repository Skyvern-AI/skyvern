export const QUESTIONNAIRE_USER_INTENTS_V1 = [
  "fill_forms",
  "extract_data",
  "monitor_website",
  "something_else",
] as const;

export type QuestionnaireUserIntentV1 =
  (typeof QUESTIONNAIRE_USER_INTENTS_V1)[number];

export function isQuestionnaireUserIntentV1(
  value: string | null,
): value is QuestionnaireUserIntentV1 {
  return (
    value !== null &&
    QUESTIONNAIRE_USER_INTENTS_V1.some((intent) => intent === value)
  );
}

export type QuestionnaireRoleV1 =
  | "developer"
  | "technical_operator"
  | "business_operator"
  | "product_manager"
  | "founder_or_executive"
  | "other"
  | "prefer_not_to_say";

export type QuestionnaireCompanyContextV1 =
  | "personal_or_individual"
  | "startup"
  | "agency_or_services"
  | "established_company"
  | "education_or_research"
  | "other"
  | "prefer_not_to_say";

export type QuestionnaireScaleIntentV1 =
  | "exploring"
  | "single_workflow"
  | "recurring_individual"
  | "team_or_multi_workflow"
  | "production_high_volume"
  | "unsure";

export type QuestionnaireReferralSourceV1 =
  | "search"
  | "social"
  | "ai_assistant"
  | "friend_or_colleague"
  | "video"
  | "blog_or_article"
  | "event_or_community"
  | "podcast"
  | "other"
  | "prefer_not_to_say";

export type QuestionnaireAnswersV1 = {
  role: QuestionnaireRoleV1;
  company_context: QuestionnaireCompanyContextV1;
  scale_intent: QuestionnaireScaleIntentV1;
  referral_source: QuestionnaireReferralSourceV1;
};

export type QuestionnairePatchV1 =
  | ({
      version: 1;
      mutation_id: string;
      expected_revision: number;
      action: "complete" | "update";
    } & QuestionnaireAnswersV1)
  | {
      version: 1;
      mutation_id: string;
      expected_revision: number;
      action: "skip";
    };

export type QuestionnairePromptPatchV1 = { version: 1; action: "reserve" };

export type QuestionnairePromptResultV1 =
  | { status: "reserved"; prompted_at: string }
  | { status: "flag_disabled" | "ineligible" | "already_prompted" };

export type QuestionnaireStatusV1 = "completed" | "skipped" | "deferred";

export type QuestionnaireStateV1 = {
  version: 1;
  response_id: string;
  revision: number;
  last_mutation_id: string;
  status: QuestionnaireStatusV1;
  role: QuestionnaireRoleV1 | null;
  company_context: QuestionnaireCompanyContextV1 | null;
  scale_intent: QuestionnaireScaleIntentV1 | null;
  referral_source: QuestionnaireReferralSourceV1 | null;
  completed_at: string | null;
  skipped_at: string | null;
  deferred_at: string | null;
  updated_at: string;
  defer_prompt_count: 0 | 1;
};

export type OnboardingState = {
  tour_completed_at: string | null;
  modal_dismissed_at: string | null;
  first_save_at: string | null;
  first_run_at: string | null;
  ab_variant: string | null;
  user_intent: string | null;
  questionnaire_prompted_at?: string | null;
  questionnaire?: QuestionnaireStateV1 | null;
  seen_canvas: boolean | null;
  seen_node_adder: boolean | null;
  seen_sidebar: boolean | null;
  seen_save_run: boolean | null;
  seen_hint_block?: boolean | null;
  seen_hint_run?: boolean | null;
  seen_hint_template?: boolean | null;
};

export type RecoveryGuidanceAssignment = {
  experiment_version: string;
  organization_id: string;
  eligible_run_id: string;
  eligible_at: string;
  arm: "control" | "treatment";
};

export type OnboardingStateResponse = {
  onboarding_state: OnboardingState;
  launch_date_at_signup: string | null;
  recovery_guidance_assignment: RecoveryGuidanceAssignment | null;
  questionnaire_prompt_result?: QuestionnairePromptResultV1 | null;
};

export type ConfirmedWriteCode =
  | "questionnaire_revision_conflict"
  | "questionnaire_requires_user_intent"
  | "questionnaire_update_requires_response"
  | "questionnaire_invalid_transition"
  | "onboarding_questionnaire_disabled"
  | "unknown";

export type ConfirmedWriteResult =
  | OnboardingStateResponse
  | { code: ConfirmedWriteCode };

type MutableOnboardingStatePatch = Partial<
  Omit<
    OnboardingState,
    | "first_save_at"
    | "first_run_at"
    | "questionnaire"
    | "questionnaire_prompted_at"
  >
>;

export type LegacyOnboardingStatePatch = MutableOnboardingStatePatch & {
  questionnaire?: never;
  questionnaire_prompt?: never;
};

export type QuestionnaireOnlyOnboardingStatePatch = {
  questionnaire: QuestionnairePatchV1;
  questionnaire_prompt?: never;
} & {
  [Key in keyof MutableOnboardingStatePatch]?: never;
};

export type QuestionnairePromptOnlyOnboardingStatePatch = {
  questionnaire?: never;
  questionnaire_prompt: QuestionnairePromptPatchV1;
} & {
  [Key in keyof MutableOnboardingStatePatch]?: never;
};

export type ConfirmedPatch =
  | QuestionnaireOnlyOnboardingStatePatch
  | QuestionnairePromptOnlyOnboardingStatePatch
  | LegacyOnboardingStatePatch;
