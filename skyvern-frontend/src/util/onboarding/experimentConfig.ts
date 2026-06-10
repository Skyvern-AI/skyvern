import { EDITOR_ONBOARDING_TOUR_FLAG } from "@/util/featureFlags";

// -- Variants --

export type ABVariant = "template-first" | "copilot-first";

export const VARIANTS = {
  TEMPLATE_FIRST: "template-first" as const,
  COPILOT_FIRST: "copilot-first" as const,
};

export const VARIANT_LIST: readonly ABVariant[] = [
  VARIANTS.TEMPLATE_FIRST,
  VARIANTS.COPILOT_FIRST,
];

export const DEFAULT_VARIANT: ABVariant = VARIANTS.TEMPLATE_FIRST;

// -- Experiment metadata --

export const EXPERIMENT = {
  name: "onboarding-template-vs-copilot",
  flagKey: EDITOR_ONBOARDING_TOUR_FLAG,
  description:
    "Measures whether template-first or copilot-first onboarding drives higher activation",
  variants: VARIANT_LIST,
  rolloutWeights: { "template-first": 50, "copilot-first": 50 } as const,
} as const;

// -- Conversion funnel --

export type FunnelStep = {
  order: number;
  event: string;
  label: string;
};

export const ACTIVATION_FUNNEL: readonly FunnelStep[] = [
  { order: 1, event: "onboarding.flow_started", label: "Flow started" },
  { order: 2, event: "onboarding.flow_completed", label: "Flow completed" },
  {
    order: 3,
    event: "onboarding.first_workflow_created",
    label: "First workflow saved",
  },
  {
    order: 4,
    event: "onboarding.first_run_completed",
    label: "First run completed",
  },
] as const;

// -- Metrics --

export type MetricKind = "funnel" | "duration" | "retention";
export type MetricPriority = "primary" | "secondary";

export type MetricDefinition = {
  id: string;
  label: string;
  priority: MetricPriority;
  kind: MetricKind;
  entryEvent: string;
  successEvent: string;
  windowDays: number;
};

export const METRICS: readonly MetricDefinition[] = [
  {
    id: "activation_rate",
    label: "Activation rate (signup to first run)",
    priority: "primary",
    kind: "funnel",
    entryEvent: "$pageview",
    successEvent: "onboarding.first_run_completed",
    windowDays: 14,
  },
  {
    id: "time_to_first_value",
    label: "Time to first value",
    priority: "secondary",
    kind: "duration",
    entryEvent: "onboarding.flow_started",
    successEvent: "onboarding.first_run_completed",
    windowDays: 14,
  },
  {
    id: "onboarding_completion_rate",
    label: "Onboarding completion rate",
    priority: "secondary",
    kind: "funnel",
    entryEvent: "onboarding.flow_started",
    successEvent: "onboarding.flow_completed",
    windowDays: 7,
  },
  {
    id: "seven_day_retention",
    label: "7-day retention",
    priority: "secondary",
    kind: "retention",
    entryEvent: "onboarding.first_run_completed",
    successEvent: "$pageview",
    windowDays: 7,
  },
] as const;

// -- Power calculation --

export const POWER_ANALYSIS = {
  baselineActivationRate: 0.25,
  minimumDetectableEffect: 0.15,
  significanceLevel: 0.05,
  statisticalPower: 0.8,
  samplesPerVariant: 2200,
  totalSamplesRequired: 4400,
  estimatedDurationWeeks: 6,
} as const;

// -- Helpers --

export function isABVariant(value: unknown): value is ABVariant {
  return value === VARIANTS.TEMPLATE_FIRST || value === VARIANTS.COPILOT_FIRST;
}
