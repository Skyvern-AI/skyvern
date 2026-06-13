import { useCallback, useEffect, useRef } from "react";
import { driver, type Config } from "driver.js";
import { useLocation } from "react-router-dom";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { EXPERIMENT, isABVariant } from "@/util/onboarding/experimentConfig";
import {
  HINT_REGISTRY,
  type ContextualHint,
} from "@/util/onboarding/contextualHints";
import type {
  OnboardingState,
  OnboardingStatePatch,
} from "@/store/onboarding/types";

const SHOW_DELAY_MS = 800;
const POLL_INTERVAL_MS = 500;
const MAX_POLLS = 10;

// Inlined rather than imported from cloud/ so this hook stays in the OSS-synced src/ tree.
const BASE_DRIVER_CONFIG: Config = {
  popoverClass: "skyvern-onboarding",
  overlayOpacity: 0.5,
  animate: true,
  smoothScroll: true,
  allowClose: true,
  allowKeyboardControl: true,
  stagePadding: 8,
  stageRadius: 8,
};

function selectHint(
  pathname: string,
  state: OnboardingState,
): ContextualHint | null {
  for (const hint of HINT_REGISTRY) {
    if (!hint.matchRoute(pathname)) continue;
    if (state[hint.seenKey]) continue;
    if (!hint.prerequisite(state)) continue;
    return hint;
  }
  return null;
}

function useContextualHints(): void {
  const onboarding = useOnboardingStateOptional();
  const { pathname } = useLocation();
  const flagVariant = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const driverRef = useRef<ReturnType<typeof driver> | null>(null);
  const activeHintRef = useRef<string | null>(null);

  const showHint = useCallback(
    (hint: ContextualHint) => {
      if (activeHintRef.current) return;
      if (document.querySelector(hint.anchor) === null) return;
      activeHintRef.current = hint.id;
      try {
        const instance = driver({
          ...BASE_DRIVER_CONFIG,
          overlayOpacity: 0,
          allowClose: true,
          onCloseClick: () => {
            OnboardingTelemetry.hintDismissed(hint.surface, hint.id);
            driverRef.current?.destroy();
          },
          onDestroyed: () => {
            activeHintRef.current = null;
            driverRef.current = null;
          },
        });
        instance.highlight({
          element: hint.anchor,
          popover: {
            title: hint.popover.title,
            description: hint.popover.description,
            side: hint.popover.side,
            align: hint.popover.align,
          },
        });
        driverRef.current = instance;
        // Persist "seen" at show time so a flaky close handler can never
        // resurrect a dismissed hint (show-once frequency cap).
        onboarding?.updateState({
          [hint.seenKey]: true,
        } as OnboardingStatePatch);
        OnboardingTelemetry.hintShown(hint.surface, hint.id);
      } catch {
        activeHintRef.current = null;
      }
    },
    [onboarding],
  );

  const state = onboarding?.state ?? null;
  const eligible =
    onboarding !== null &&
    !onboarding.isLoading &&
    state !== null &&
    state.tour_completed_at !== null &&
    isABVariant(flagVariant);

  useEffect(() => {
    if (!eligible || state === null) return;
    if (activeHintRef.current) return;
    const hint = selectHint(pathname, state);
    if (!hint) return;

    let polls = 0;
    let showTimeout: ReturnType<typeof setTimeout> | null = null;
    const interval = setInterval(() => {
      polls += 1;
      if (document.querySelector(hint.anchor) !== null) {
        clearInterval(interval);
        showTimeout = setTimeout(() => showHint(hint), SHOW_DELAY_MS);
      } else if (polls >= MAX_POLLS) {
        clearInterval(interval);
      }
    }, POLL_INTERVAL_MS);

    return () => {
      clearInterval(interval);
      if (showTimeout) clearTimeout(showTimeout);
    };
  }, [eligible, pathname, state, showHint]);

  // Tear down an active hint when navigating away from its route.
  useEffect(() => {
    return () => {
      driverRef.current?.destroy();
    };
  }, [pathname]);

  // Final unmount cleanup.
  useEffect(() => {
    return () => {
      driverRef.current?.destroy();
    };
  }, []);
}

export { useContextualHints, selectHint };
