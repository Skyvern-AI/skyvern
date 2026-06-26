import { statusIsFinalized } from "@/routes/tasks/types";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";

type Params = {
  cacheKey: string;
  cacheKeyValue: string;
  workflowPermanentId: string | undefined;
  workflowRunId?: string;
};

export function useIsGeneratingCode({
  cacheKey,
  cacheKeyValue,
  workflowPermanentId,
  workflowRunId,
}: Params): boolean {
  const { data: workflowRun } = useWorkflowRunQuery(
    workflowRunId ? { workflowRunId } : undefined,
  );
  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    status: "published",
  });

  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  const publishedLabelCount = Object.keys(
    blockScriptsPublished?.blocks ?? {},
  ).length;
  const hasPublishedScript =
    publishedLabelCount > 0 || Boolean(blockScriptsPublished?.main_script);

  return !hasPublishedScript && !isFinalized && Boolean(workflowRun);
}
