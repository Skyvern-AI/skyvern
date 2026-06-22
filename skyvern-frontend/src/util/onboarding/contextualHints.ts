import type { OnboardingState } from "@/store/onboarding/types";
import type { Surface } from "@/util/onboarding/OnboardingTelemetry";

export type HintSeenKey =
  | "seen_hint_block"
  | "seen_hint_run"
  | "seen_hint_template";

export type ContextualHint = {
  id: string;
  surface: Surface;
  seenKey: HintSeenKey;
  anchor: string;
  matchRoute: (pathname: string) => boolean;
  prerequisite: (state: OnboardingState) => boolean;
  popover: {
    title: string;
    description: string;
    side: "top" | "bottom" | "left" | "right";
    align: "start" | "center" | "end";
  };
};

const EDITOR_ROUTE = /^\/workflows\/[^/]+\/build$/;

export const HINT_REGISTRY: readonly ContextualHint[] = [
  {
    id: "add-another-block",
    surface: "editor",
    seenKey: "seen_hint_block",
    anchor: "[data-tour='node-adder']",
    matchRoute: (pathname) => EDITOR_ROUTE.test(pathname),
    prerequisite: () => true,
    popover: {
      title: "Add another block",
      description:
        "Chain steps together - use the + to add another block and build a multi-step workflow.",
      side: "left",
      align: "center",
    },
  },
  {
    id: "run-recording",
    surface: "runs",
    seenKey: "seen_hint_run",
    anchor: "[data-hint='run-recording']",
    matchRoute: (pathname) => pathname === "/runs",
    prerequisite: () => true,
    popover: {
      title: "Watch it run",
      description:
        "Click any run to watch the live browser recording and inspect each step.",
      side: "bottom",
      align: "start",
    },
  },
  {
    id: "start-template",
    surface: "dashboard",
    seenKey: "seen_hint_template",
    anchor: "[data-hint='start-template']",
    matchRoute: (pathname) => pathname === "/workflows",
    prerequisite: (state) => state.first_save_at === null,
    popover: {
      title: "Start from a template",
      description:
        "Not sure where to begin? Browse ready-made templates for common automations.",
      side: "top",
      align: "center",
    },
  },
] as const;
