export type OnboardingState = {
  tour_completed_at: string | null;
  modal_dismissed_at: string | null;
  first_save_at: string | null;
  first_run_at: string | null;
  ab_variant: string | null;
  user_intent: string | null;
  seen_canvas: boolean | null;
  seen_node_adder: boolean | null;
  seen_sidebar: boolean | null;
  seen_save_run: boolean | null;
  seen_hint_block?: boolean | null;
  seen_hint_run?: boolean | null;
  seen_hint_template?: boolean | null;
};

export type OnboardingStateResponse = {
  onboarding_state?: OnboardingState;
  launch_date_at_signup: string | null;
};

export type OnboardingStatePatch = Partial<OnboardingState>;
