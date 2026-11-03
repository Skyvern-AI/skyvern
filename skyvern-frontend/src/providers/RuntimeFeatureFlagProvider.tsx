import { useCallback, type PropsWithChildren } from "react";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useRuntimeConfig } from "@/hooks/useRuntimeConfig";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";

function RuntimeFeatureFlagProvider({ children }: PropsWithChildren) {
  const { data } = useRuntimeConfig();
  const evaluate = useCallback(
    (flagName: string): boolean | undefined => {
      if (flagName === "WORKFLOW_COPILOT_CODE_BLOCK_MODE") {
        return data?.workflow_copilot_code_block_mode;
      }
      if (flagName === "CODE_BLOCK_ACCESS") {
        return data?.code_block_access;
      }
      if (flagName === "ENABLE_DISCOVER_COPILOT_HANDOFF") {
        return false;
      }
      // The OSS backend never gates tagging (is_workflow_tagging_enabled is always true).
      if (flagName === WORKFLOW_TAGGING_FLAG) {
        return true;
      }
      return undefined;
    },
    [data],
  );

  return (
    <FeatureFlagContext.Provider value={evaluate}>
      {children}
    </FeatureFlagContext.Provider>
  );
}

export { RuntimeFeatureFlagProvider };
