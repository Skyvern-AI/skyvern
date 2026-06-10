import { createContext, useContext } from "react";
import type { OnboardingState, OnboardingStatePatch } from "./types";

type OnboardingContextValue = {
  state: OnboardingState | null;
  isLoading: boolean;
  updateState: (patch: OnboardingStatePatch) => void;
  isNewUser: boolean;
  abVariant: string | null;
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
export type { OnboardingContextValue };
