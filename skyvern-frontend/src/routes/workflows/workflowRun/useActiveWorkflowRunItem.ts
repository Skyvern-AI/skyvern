import { useSearchParams } from "react-router-dom";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import {
  getRunAttempt,
  runIsLogicallyFinal,
} from "@/routes/workflows/workflowRun/runRetryState";
import {
  filterTimelineToAttempt,
  findActiveItem,
} from "./workflowTimelineUtils";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

function useActiveWorkflowRunItem(): [
  WorkflowRunOverviewActiveElement,
  (item: string) => void,
] {
  const [searchParams, setSearchParams] = useSearchParams();
  const active = searchParams.get("active");

  const { data: workflowRunWithWorkflow } = useWorkflowRunWithWorkflowQuery();

  const { data: workflowRunTimeline } = useWorkflowRunTimelineQuery();

  const workflowRunIsFinalized =
    workflowRunWithWorkflow && runIsLogicallyFinal(workflowRunWithWorkflow);
  const finallyBlockLabel =
    workflowRunWithWorkflow?.workflow?.workflow_definition
      ?.finally_block_label ?? null;
  const currentAttemptTimeline = filterTimelineToAttempt(
    workflowRunTimeline ?? [],
    workflowRunWithWorkflow?.attempts ?? [],
    getRunAttempt(workflowRunWithWorkflow ?? {}),
  );
  const activeItem = findActiveItem(
    active === null ? currentAttemptTimeline : (workflowRunTimeline ?? []),
    active,
    !!workflowRunIsFinalized,
    finallyBlockLabel,
  );

  function handleSetActiveItem(id: string) {
    searchParams.set("active", id);
    setSearchParams(searchParams, {
      replace: true,
    });
  }

  return [activeItem, handleSetActiveItem];
}

export { useActiveWorkflowRunItem };
