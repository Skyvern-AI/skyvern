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
