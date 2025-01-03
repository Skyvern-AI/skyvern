import { useSearchParams } from "react-router-dom";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { statusIsFinalized } from "@/routes/tasks/types";
import { findActiveItem } from "./workflowTimelineUtils";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

function useActiveWorkflowRunItem(): [
  WorkflowRunOverviewActiveElement,
  (item: string) => void,
] {
  const [searchParams, setSearchParams] = useSearchParams();
  const active = searchParams.get("active");

  const { data: workflowRun } = useWorkflowRunQuery();

  const { data: workflowRunTimeline } = useWorkflowRunTimelineQuery();

  const workflowRunIsFinalized = workflowRun && statusIsFinalized(workflowRun);
  const activeItem = findActiveItem(
    workflowRunTimeline ?? [],
    active,
    !!workflowRunIsFinalized,
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
