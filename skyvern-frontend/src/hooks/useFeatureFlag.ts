import { createContext, useContext } from "react";

/**
 * Context for feature flag evaluation. Defaults to a no-op that always
 * returns undefined (OSS behavior). The cloud build provides the real
 * evaluator via FeatureFlagContext.Provider in cloud/Providers.tsx.
 */
export const FeatureFlagContext = createContext<
  (flagName: string) => boolean | undefined
>(() => undefined);

export function useFeatureFlag(flagName: string): boolean | undefined {
  const evaluate = useContext(FeatureFlagContext);
  return evaluate(flagName);
}

/**
 * Context for multivariate (string variant) feature flags. Mirrors
 * FeatureFlagContext; the cloud build supplies the real evaluator.
 */
export const FeatureFlagValueContext = createContext<
  (flagName: string) => string | undefined
>(() => undefined);

export function useFeatureFlagValue(flagName: string): string | undefined {
  const evaluate = useContext(FeatureFlagValueContext);
  return evaluate(flagName);
}
