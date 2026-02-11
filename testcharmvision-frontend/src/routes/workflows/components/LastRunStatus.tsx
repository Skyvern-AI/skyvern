import { BadgeLoading } from "@/components/BadgeLoading";
import { StatusBadge } from "@/components/StatusBadge";
import { useWorkflowLastRunQuery } from "../hooks/useWorkflowLastRunQuery";

type Props = {
  workflowId: string;
};

function LastRunStatus({ workflowId }: Props) {
  const { data, isLoading } = useWorkflowLastRunQuery({ workflowId });

  if (isLoading) {
    return <BadgeLoading />;
  }

  if (!data) {
    return null;
  }

  if (data.status === "N/A") {
    return <span>N/A</span>;
  }

  return <StatusBadge status={data.status} />;
}

export { LastRunStatus };
