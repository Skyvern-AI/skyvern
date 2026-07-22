import { useSearchParams } from "react-router-dom";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { statusIsFinalized } from "@/routes/tasks/types";
import { findActiveItem } from "./workflowTimelineUtils";
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
    workflowRunWithWorkflow && statusIsFinalized(workflowRunWithWorkflow);
  const finallyBlockLabel =
    workflowRunWithWorkflow?.workflow?.workflow_definition
      ?.finally_block_label ?? null;
  const activeItem = findActiveItem(
    workflowRunTimeline ?? [],
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
