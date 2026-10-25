import { createContext, useContext, useEffect, useState } from "react";
import posthog from "posthog-js";

export const PostHogInitializedContext = createContext(true);

export function usePostHogFeatureFlagEnabled(
  flag: string,
): boolean | undefined {
  const initialized = useContext(PostHogInitializedContext);
  const [enabled, setEnabled] = useState<boolean>();

  useEffect(() => {
    if (!initialized) return;

    const update = () => setEnabled(posthog.isFeatureEnabled(flag));
    update();
    return posthog.onFeatureFlags(update);
  }, [flag, initialized]);

  return enabled;
}

export function usePostHogFeatureFlagVariantKey(
  flag: string,
): boolean | string | undefined {
  const initialized = useContext(PostHogInitializedContext);
  const [variant, setVariant] = useState<boolean | string>();

  useEffect(() => {
    if (!initialized) return;

    const update = () => setVariant(posthog.getFeatureFlag(flag));
    update();
    return posthog.onFeatureFlags(update);
  }, [flag, initialized]);

  return variant;
}
