import { EDITOR_ONBOARDING_TOUR_FLAG } from "@/util/featureFlags";
import {
  ACTIVATION_FUNNEL,
  METRICS,
  type MetricDefinition,
} from "./experimentConfig";

// -- Ramp stages --

export type RampStage = {
  name: string;
  percentage: number;
  holdDays: number;
  gateCheck: boolean;
};

export const RAMP_STAGES: readonly RampStage[] = [
  { name: "dark-launch", percentage: 0, holdDays: 0, gateCheck: false },
  { name: "canary", percentage: 10, holdDays: 1, gateCheck: true },
  { name: "broad", percentage: 50, holdDays: 3, gateCheck: true },
  { name: "ga", percentage: 100, holdDays: 0, gateCheck: false },
] as const;

// -- Guardrail thresholds --

export type GuardrailMetric = {
  id: string;
  label: string;
  event: string;
  threshold: number;
  direction: "above" | "below";
  windowMinutes: number;
};

export const GUARDRAIL_METRICS: readonly GuardrailMetric[] = [
  {
    id: "onboarding_error_rate",
    label: "Onboarding JS error rate",
    event: "onboarding.error",
    threshold: 0.05,
    direction: "above",
    windowMinutes: 60,
  },
  {
    id: "modal_render_failure_rate",
    label: "Modal render failure rate",
    event: "onboarding.modal_render_error",
    threshold: 0.05,
    direction: "above",
    windowMinutes: 60,
  },
  {
    id: "tour_js_error_rate",
    label: "Tour JS error rate",
    event: "onboarding.tour_error",
    threshold: 0.05,
    direction: "above",
    windowMinutes: 60,
  },
  {
    id: "completion_rate_drop",
    label: "Completion rate drop from baseline",
    event: "onboarding.flow_completed",
    threshold: 0.2,
    direction: "below",
    windowMinutes: 1440,
  },
] as const;

// -- Rollback criteria --

export type RollbackRule = {
  metric: string;
  condition: string;
  action: "auto-rollback" | "alert-only";
};

export const ROLLBACK_RULES: readonly RollbackRule[] = [
  {
    metric: "onboarding_error_rate",
    condition: "error rate > 5% over 1h window",
    action: "auto-rollback",
  },
  {
    metric: "modal_render_failure_rate",
    condition: "modal render failures > 5% over 1h window",
    action: "auto-rollback",
  },
  {
    metric: "tour_js_error_rate",
    condition: "tour JS errors > 5% over 1h window",
    action: "auto-rollback",
  },
  {
    metric: "completion_rate_drop",
    condition: "completion rate drops > 20% from baseline over 24h window",
    action: "auto-rollback",
  },
] as const;

// -- PostHog dashboard config --

export type DashboardWidget = {
  id: string;
  title: string;
  kind: "funnel" | "trend" | "distribution" | "retention";
  events: readonly string[];
  breakdownBy?: string;
  dateRange: string;
};

export const DASHBOARD_WIDGETS: readonly DashboardWidget[] = [
  {
    id: "activation_funnel_by_variant",
    title: "Activation funnel by variant",
    kind: "funnel",
    events: ACTIVATION_FUNNEL.map((s) => s.event),
    breakdownBy: "variant",
    dateRange: "last 14 days",
  },
  {
    id: "tour_completion_rate",
    title: "Tour completion rate",
    kind: "funnel",
    events: ["onboarding.tour_started", "onboarding.tour_completed"],
    breakdownBy: "variant",
    dateRange: "last 14 days",
  },
  {
    id: "modal_skip_rate",
    title: "Modal skip rate",
    kind: "trend",
    events: ["onboarding.modal_opened", "onboarding.modal_skipped"],
    breakdownBy: "variant",
    dateRange: "last 14 days",
  },
  {
    id: "time_to_first_value_distribution",
    title: "Time-to-first-value distribution",
    kind: "distribution",
    events: ["onboarding.flow_started", "onboarding.first_run_completed"],
    breakdownBy: "variant",
    dateRange: "last 14 days",
  },
  {
    id: "error_rate_trend",
    title: "Onboarding error rate trend",
    kind: "trend",
    events: [
      "onboarding.error",
      "onboarding.modal_render_error",
      "onboarding.tour_error",
    ],
    dateRange: "last 7 days",
  },
  {
    id: "seven_day_retention_by_variant",
    title: "7-day retention by variant",
    kind: "retention",
    events: ["onboarding.first_run_completed", "$pageview"],
    breakdownBy: "variant",
    dateRange: "last 30 days",
  },
] as const;

// -- Aggregate config --

export const ROLLOUT_CONFIG = {
  flagKey: EDITOR_ONBOARDING_TOUR_FLAG,
  stages: RAMP_STAGES,
  guardrails: GUARDRAIL_METRICS,
  rollbackRules: ROLLBACK_RULES,
  dashboard: DASHBOARD_WIDGETS,
  metrics: METRICS,
} as const;

// -- Helpers --

export function getAutoRollbackRules(): readonly RollbackRule[] {
  return ROLLBACK_RULES.filter((r) => r.action === "auto-rollback");
}

export function getGuardrailForMetric(
  metricId: string,
): GuardrailMetric | undefined {
  return GUARDRAIL_METRICS.find((g) => g.id === metricId);
}

export function getMetricDefinition(
  metricId: string,
): MetricDefinition | undefined {
  return METRICS.find((m) => m.id === metricId);
}

export function isGateCheckRequired(stageIndex: number): boolean {
  const stage = RAMP_STAGES[stageIndex];
  return stage?.gateCheck ?? false;
}
