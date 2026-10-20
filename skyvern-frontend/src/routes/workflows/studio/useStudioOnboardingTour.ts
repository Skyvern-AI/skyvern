import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { driver, type DriveStep } from "driver.js";

import { BASE_DRIVER_CONFIG } from "@/hooks/useEditorOnboardingTour";
import { useStudioRunRouteMatch } from "@/routes/workflows/useStudioRunRouteMatch";
import { useAutoplayStore } from "@/store/useAutoplayStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import {
  isOnboardingReplayRequested,
  setOnboardingReplay,
} from "@/util/onboarding/onboardingReplay";
import "@/util/onboarding/product-tour.css";

import { studioPanelId } from "./constants";
import { liveSearch } from "./liveSearch";

const SURFACE = "studio" as const;
const AUTO_START_DELAY_MS = 1500;

const TOUR_STEPS: { name: string; step: DriveStep }[] = [
  {
    name: "copilot",
    step: {
      element: `#${studioPanelId("copilot")}`,
      popover: {
        title: "Meet Copilot",
        description:
          "Tell Copilot what to automate or what to change. It builds and tests the steps for you.",
        side: "right",
        align: "center",
      },
    },
  },
  {
    name: "browser",
    step: {
      element: `#${studioPanelId("browser")}`,
      popover: {
        title: "Watch it work",
        description:
          "Your agent works in this browser. Watch it here and take control whenever it needs a hand.",
        side: "left",
        align: "center",
      },
    },
  },
  {
    name: "pane_toggles",
    step: {
      element: "[data-tour='studio-pane-toggles']",
      popover: {
        title: "Open more panes",
        description:
          "These toggles show or hide panes side by side, including the Editor with your agent's blocks.",
        side: "bottom",
        align: "center",
      },
    },
  },
];

// Closed panes stay mounted with display:none, so presence alone isn't enough.
function isShown(selector: string): boolean {
  const el = document.querySelector(selector);
  return el !== null && el.checkVisibility?.() !== false;
}

// Entry is read once on mount: Copilot strips ?via=discover and autoplay clears
// itself once consumed. Shared run/block links never start the tour; a Home
// prompt waits for its run's browser to go live; a directly created agent starts now.
function useStudioOnboardingTour(
  enabled: boolean,
  workflowPermanentId: string,
): void {
  const onboarding = useOnboardingStateOptional();
  const location = useLocation();
  const runRouteMatch = useStudioRunRouteMatch();
  const [{ deepLinked, fromHome, replay }] = useState(() => {
    const params = new URLSearchParams(liveSearch(location.search));
    return {
      deepLinked: Boolean(
        params.get("wr") ||
        params.get("active") ||
        params.get("bl") ||
        runRouteMatch,
      ),
      fromHome:
        params.get("via") === "discover" ||
        useAutoplayStore.getState().wpid === workflowPermanentId,
      replay: isOnboardingReplayRequested(),
    };
  });
  // Only the debug stream: Home runs drive the debug browser, and runStream is not
  // cleared between agents, so a stale one could start the tour early.
  const browserLive = useStudioBrowserStore(
    (s) => s.debugStream?.state === "live",
  );
  const startedRef = useRef(false);
  const tourRef = useRef<ReturnType<typeof driver> | null>(null);

  const shouldStart =
    enabled &&
    !deepLinked &&
    (!fromHome || browserLive) &&
    onboarding !== null &&
    !onboarding.isLoading &&
    onboarding.state !== null &&
    (replay ||
      (onboarding.isNewUser &&
        onboarding.state.first_run_at == null &&
        onboarding.state.studio_tour_completed_at == null));

  const updateStateRef = useRef(onboarding?.updateState);
  updateStateRef.current = onboarding?.updateState;

  useEffect(() => {
    if (!shouldStart || startedRef.current) return;

    const timeout = setTimeout(() => {
      const steps = TOUR_STEPS.filter(({ step }) =>
        isShown(step.element as string),
      );
      if (steps.length === 0) return;
      startedRef.current = true;

      let lastIndex = 0;
      let completed = false;
      const tour = driver({
        ...BASE_DRIVER_CONFIG,
        showProgress: true,
        progressText: "Step {{current}} of {{total}}",
        steps: steps.map(({ step }) => step),
        onHighlightStarted: (_el, _step, { driver: d }) => {
          lastIndex = d.getActiveIndex() ?? 0;
          if (replay) return;
          OnboardingTelemetry.tourStepViewed(
            SURFACE,
            steps[lastIndex]!.name,
            lastIndex,
            1,
          );
        },
        onNextClick: (_el, _step, { driver: d }) => {
          if (d.isLastStep()) {
            completed = true;
            d.destroy();
          } else {
            d.moveNext();
          }
        },
        // Unmount nulls tourRef before destroying, so leaving the page mid-tour
        // records nothing; only a user close counts as seen.
        onDestroyed: () => {
          if (tourRef.current === null) return;
          tourRef.current = null;
          // A replay keeps the first-run funnel and completion time untouched.
          if (replay) {
            setOnboardingReplay(false);
            return;
          }
          if (completed) {
            OnboardingTelemetry.tourCompleted(SURFACE);
          } else {
            OnboardingTelemetry.tourDismissed(SURFACE, steps[lastIndex]!.name);
          }
          updateStateRef.current?.({
            studio_tour_completed_at: new Date().toISOString(),
          });
        },
      });
      tourRef.current = tour;
      if (!replay) OnboardingTelemetry.tourStarted(SURFACE);
      tour.drive();
    }, AUTO_START_DELAY_MS);

    return () => clearTimeout(timeout);
  }, [shouldStart, replay]);

  useEffect(
    () => () => {
      const tour = tourRef.current;
      tourRef.current = null;
      tour?.destroy();
    },
    [],
  );
}

export { useStudioOnboardingTour };
