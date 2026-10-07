import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";

type WorkflowTaggingState = "pending" | "on" | "off";

// Tag routes 403 for every org with the flag off, so only a resolved `true` turns tagging on. The cloud provider
// resolves the flag even when `/customer` fails, so `undefined` here means the flag is still loading.
function useWorkflowTaggingState(): WorkflowTaggingState {
  const flag = useFeatureFlag(WORKFLOW_TAGGING_FLAG);
  if (flag === undefined) {
    return "pending";
  }
  return flag ? "on" : "off";
}

function useWorkflowTaggingEnabled(): boolean {
  return useWorkflowTaggingState() === "on";
}

// `hold` is true while the flag is pending and the URL carries a filter; callers keep their query disabled so a
// tag-filtered link never loads unfiltered data first. `tags` drops the filter only once tagging is known to be off,
// so a held query's key never matches a cached unfiltered result.
function useUrlTagFilter(serialized: string | undefined): {
  tags: string;
  hold: boolean;
} {
  const state = useWorkflowTaggingState();
  const filter = serialized ?? "";
  return {
    tags: state === "off" ? "" : filter,
    hold: state === "pending" && filter !== "",
  };
}

export { useUrlTagFilter, useWorkflowTaggingEnabled, useWorkflowTaggingState };
export type { WorkflowTaggingState };
