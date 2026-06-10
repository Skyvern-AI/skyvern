import { useCallback, useEffect, useRef, useState } from "react";
import { driver, type DriveStep, type Config } from "driver.js";
import { useFeatureFlagVariantKey } from "posthog-js/react";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { useProductTourStore } from "@/store/ProductTourStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { EXPERIMENT, isABVariant } from "@/util/onboarding/experimentConfig";
import type { OnboardingStatePatch } from "@/store/onboarding/types";

const SURFACE = "editor" as const;
const LAYER = 1 as const;
const AUTO_START_DELAY_MS = 1500;

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

const TOUR_STEPS: DriveStep[] = [
  {
    element: "[data-tour='editor-canvas']",
    popover: {
      title: "Your workflow canvas",
      description: "Drag blocks here to compose your automation.",
      side: "left",
      align: "center",
    },
  },
  {
    element: "[data-tour='node-adder']",
    popover: {
      title: "Add blocks",
      description: "Click the + button to add your first block.",
      side: "left",
      align: "center",
      showButtons: ["previous", "close"],
    },
  },
  {
    element: "[data-tour='sidebar-region']",
    popover: {
      title: "Block settings",
      description:
        "Click any block to configure its settings in this side panel.",
      side: "left",
      align: "center",
    },
  },
  {
    element: "[data-tour='editor-actions']",
    popover: {
      title: "Save and run",
      description: "Save your work and run it to see results.",
      side: "bottom",
      align: "end",
    },
  },
];

const STEP_NAMES = ["canvas", "node_adder", "sidebar", "save_run"] as const;

const NODE_ADDER_INDEX = STEP_NAMES.indexOf("node_adder");

const NODE_ADDER_SELECTOR = "[data-tour='node-adder']";

// driver.js reads a step's showButtons fresh on each render, so toggling this
// reflects on the next time step 2 is shown (e.g. Previous back to it).
function setNodeAdderNextVisible(visible: boolean): void {
  const popover = TOUR_STEPS[NODE_ADDER_INDEX]?.popover;
  if (popover) {
    popover.showButtons = visible
      ? ["next", "previous", "close"]
      : ["previous", "close"];
  }
}

const SEEN_PATCHES: Record<(typeof STEP_NAMES)[number], OnboardingStatePatch> =
  {
    canvas: { seen_canvas: true },
    node_adder: { seen_node_adder: true },
    sidebar: { seen_sidebar: true },
    save_run: { seen_save_run: true },
  };

// driver.js hands callbacks a shallow clone of the active step, so match by element selector, never by object identity.
function indexOfStep(steps: DriveStep[], step: DriveStep): number {
  if (step.element == null) return -1;
  return steps.findIndex((s) => s.element === step.element);
}

function filterAvailableSteps(steps: DriveStep[]): DriveStep[] {
  return steps.filter((step) => {
    if (typeof step.element !== "string") return true;
    return document.querySelector(step.element) !== null;
  });
}

type UseEditorOnboardingTourReturn = {
  showExitDialog: boolean;
  onExitConfirm: () => void;
  onExitCancel: () => void;
};

function useEditorOnboardingTour(): UseEditorOnboardingTourReturn {
  const onboarding = useOnboardingStateOptional();
  const flagVariant = useFeatureFlagVariantKey(EXPERIMENT.flagKey);
  const [showExitDialog, setShowExitDialog] = useState(false);
  const [tourActive, setTourActive] = useState(false);
  const driverRef = useRef<ReturnType<typeof driver> | null>(null);
  const currentStepRef = useRef(0);
  const tourActiveRef = useRef(false);
  const requestedAt = useProductTourStore((s) => s.requestedAt);
  const clearRequest = useProductTourStore((s) => s.clearRequest);
  const panelActive = useWorkflowPanelStore((s) => s.workflowPanelState.active);
  const panelContent = useWorkflowPanelStore(
    (s) => s.workflowPanelState.content,
  );
  const availableStepsRef = useRef<DriveStep[]>([]);
  const nodeAdderGateSatisfiedRef = useRef(false);
  const tourStartedEmittedRef = useRef(false);

  const shouldAutoStart =
    onboarding !== null &&
    !onboarding.isLoading &&
    onboarding.state !== null &&
    !onboarding.isNewUser &&
    onboarding.state.tour_completed_at === null &&
    isABVariant(flagVariant);

  const advanceFromStep = useCallback(
    (step: DriveStep) => {
      const availableSteps = availableStepsRef.current;
      const stepIndex = indexOfStep(TOUR_STEPS, step);
      const stepName = STEP_NAMES[stepIndex];
      const isLastStep =
        indexOfStep(availableSteps, step) === availableSteps.length - 1;

      if (stepName) {
        OnboardingTelemetry.stepCompleted(SURFACE, stepName);
      }

      if (isLastStep) {
        OnboardingTelemetry.tourCompleted(SURFACE);
        onboarding?.updateState({
          ...(stepName ? SEEN_PATCHES[stepName] : {}),
          tour_completed_at: new Date().toISOString(),
        });
        tourActiveRef.current = false;
        setTourActive(false);
        driverRef.current?.destroy();
      } else {
        if (stepName) {
          onboarding?.updateState(SEEN_PATCHES[stepName]);
        }
        driverRef.current?.moveNext();
      }
    },
    [onboarding],
  );

  const startDriver = useCallback(() => {
    if (tourActiveRef.current) return;

    const availableSteps = filterAvailableSteps(TOUR_STEPS);
    if (availableSteps.length === 0) return;

    tourActiveRef.current = true;
    setTourActive(true);
    availableStepsRef.current = availableSteps;
    nodeAdderGateSatisfiedRef.current = false;
    tourStartedEmittedRef.current = false;
    setNodeAdderNextVisible(false);

    // Existing users reach the tour without the get-started modal, where the arm
    // is normally registered/persisted, so set it here before any tour telemetry
    // so those events carry the experiment arm.
    if (
      onboarding?.state &&
      onboarding.state.ab_variant === null &&
      isABVariant(flagVariant)
    ) {
      OnboardingTelemetry.registerVariant(flagVariant);
      OnboardingTelemetry.abVariantAssigned(SURFACE, flagVariant);
      onboarding.updateState({ ab_variant: flagVariant });
    }

    const config: Config = {
      ...BASE_DRIVER_CONFIG,
      showProgress: true,
      progressText: "Step {{current}} of {{total}}",
      allowClose: false,
      steps: availableSteps,
      onHighlightStarted: (_element, step) => {
        const stepIndex = indexOfStep(TOUR_STEPS, step);
        if (stepIndex >= 0) {
          currentStepRef.current = stepIndex;
          if (
            indexOfStep(availableSteps, step) === 0 &&
            !tourStartedEmittedRef.current
          ) {
            tourStartedEmittedRef.current = true;
            OnboardingTelemetry.tourStarted(SURFACE);
          }
          OnboardingTelemetry.tourStepViewed(
            SURFACE,
            STEP_NAMES[stepIndex] ?? "unknown",
            stepIndex,
            LAYER,
          );
        }
      },
      onCloseClick: () => {
        setShowExitDialog(true);
      },
      onNextClick: (_element, step) => {
        // Until the "+" gate is satisfied, ignore Next/ArrowRight on step 2 (driver
        // routes ArrowRight here too) so it only advances via the effect below.
        if (
          indexOfStep(TOUR_STEPS, step) === NODE_ADDER_INDEX &&
          !nodeAdderGateSatisfiedRef.current
        ) {
          return;
        }
        advanceFromStep(step);
      },
    };

    try {
      const driverInstance = driver(config);
      driverInstance.drive();
      driverRef.current = driverInstance;
    } catch {
      OnboardingTelemetry.tourError(SURFACE);
      tourActiveRef.current = false;
      setTourActive(false);
    }
  }, [advanceFromStep, onboarding, flagVariant]);

  useEffect(() => {
    if (!shouldAutoStart || tourActiveRef.current) return;

    const timeout = setTimeout(() => {
      startDriver();
    }, AUTO_START_DELAY_MS);

    return () => {
      clearTimeout(timeout);
    };
  }, [shouldAutoStart, startDriver]);

  useEffect(() => {
    if (requestedAt === null) return;
    clearRequest();
    // No provider (non-cloud app) means dismissals/completions cannot persist, so
    // never start an untracked tour - mirrors the auto-start guard.
    if (onboarding === null) return;
    if (!isABVariant(flagVariant)) return;
    if (tourActiveRef.current) {
      driverRef.current?.destroy();
      tourActiveRef.current = false;
      setTourActive(false);
    }
    startDriver();
  }, [requestedAt, clearRequest, startDriver, flagVariant, onboarding]);

  // Step 2 ("Add blocks") is gated: opening the block library while it is the
  // active step satisfies the gate once and advances. Once satisfied the Next
  // button is restored so returning to step 2 (via Previous) isn't a dead end.
  useEffect(() => {
    const libraryOpen = panelActive && panelContent === "nodeLibrary";
    const onAdderStep =
      currentStepRef.current === NODE_ADDER_INDEX ||
      driverRef.current?.getActiveStep?.()?.element === NODE_ADDER_SELECTOR;

    if (
      !tourActiveRef.current ||
      nodeAdderGateSatisfiedRef.current ||
      !libraryOpen ||
      !onAdderStep
    ) {
      return;
    }
    nodeAdderGateSatisfiedRef.current = true;
    setNodeAdderNextVisible(true);
    advanceFromStep(TOUR_STEPS[NODE_ADDER_INDEX]!);
  }, [panelActive, panelContent, advanceFromStep]);

  useEffect(() => {
    return () => {
      driverRef.current?.destroy();
    };
  }, []);

  // Hide the driver.js layer while the exit dialog is open (see product-tour.css for why).
  useEffect(() => {
    if (!showExitDialog) return;
    const root = document.documentElement;
    root.classList.add("tour-paused");
    return () => {
      root.classList.remove("tour-paused");
    };
  }, [showExitDialog]);

  useEffect(() => {
    if (!tourActive) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setShowExitDialog(true);
      }
    };

    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [tourActive]);

  const onExitConfirm = useCallback(() => {
    const lastStep = STEP_NAMES[currentStepRef.current] ?? "unknown";
    OnboardingTelemetry.tourDismissed(SURFACE, lastStep);
    onboarding?.updateState({ tour_completed_at: new Date().toISOString() });
    tourActiveRef.current = false;
    setTourActive(false);
    setShowExitDialog(false);
    driverRef.current?.destroy();
  }, [onboarding]);

  const onExitCancel = useCallback(() => {
    setShowExitDialog(false);
  }, []);

  return { showExitDialog, onExitConfirm, onExitCancel };
}

export { useEditorOnboardingTour };
export type { UseEditorOnboardingTourReturn };
