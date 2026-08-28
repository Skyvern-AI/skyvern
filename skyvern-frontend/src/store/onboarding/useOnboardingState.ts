import { createContext, useContext } from "react";
import type {
  ConfirmedPatch,
  ConfirmedWriteResult,
  LegacyOnboardingStatePatch,
  OnboardingState,
  RecoveryGuidanceAssignment,
} from "./types";

type ConfirmedOnboardingStateWriter = (
  patch: ConfirmedPatch,
) => Promise<ConfirmedWriteResult>;

type OnboardingContextValue = {
  state: OnboardingState | null;
  isLoading: boolean;
  updateState: (patch: LegacyOnboardingStatePatch) => void;
  updateStateConfirmed: ConfirmedOnboardingStateWriter;
  isNewUser: boolean;
  abVariant: string | null;
  recoveryGuidanceAssignment: RecoveryGuidanceAssignment | null;
};

const OnboardingContext = createContext<OnboardingContextValue | null>(null);

function useOnboardingState(): OnboardingContextValue {
  const context = useContext(OnboardingContext);
  if (!context) {
    throw new Error(
      "useOnboardingState must be used within OnboardingProvider",
    );
  }
  return context;
}

function useOnboardingStateOptional(): OnboardingContextValue | null {
  return useContext(OnboardingContext);
}

export { OnboardingContext, useOnboardingState, useOnboardingStateOptional };
export type { ConfirmedOnboardingStateWriter, OnboardingContextValue };
