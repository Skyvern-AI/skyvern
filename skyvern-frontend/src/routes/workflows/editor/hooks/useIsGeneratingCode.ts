import { statusIsFinalized } from "@/routes/tasks/types";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { shouldPollForGeneratedCode } from "@/routes/workflows/utils";

type Params = {
  cacheKey: string;
  cacheKeyValue: string;
  workflowPermanentId: string | undefined;
  workflowRunId?: string;
};

export function useIsGeneratingCode(params: Params): boolean {
  const { cacheKey, cacheKeyValue, workflowPermanentId } = params;
  const { data: workflowRun } = useWorkflowRunQuery(
    "workflowRunId" in params
      ? { workflowRunId: params.workflowRunId }
      : undefined,
  );
  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    status: "published",
  });
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });

  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  const publishedLabelCount = Object.keys(
    blockScriptsPublished?.blocks ?? {},
  ).length;
  const hasPublishedScript =
    publishedLabelCount > 0 || Boolean(blockScriptsPublished?.main_script);

  return (
    Boolean(workflowRun) &&
    shouldPollForGeneratedCode(workflow, isFinalized, hasPublishedScript)
  );
}
