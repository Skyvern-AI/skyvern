import { Skeleton } from "@/components/ui/skeleton";
import { useWorkflowLastRunQuery } from "../hooks/useWorkflowLastRunQuery";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

type Props = {
  workflowId: string;
};

function LastRunAtTime({ workflowId }: Props) {
  const { data, isLoading } = useWorkflowLastRunQuery({ workflowId });

  if (isLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!data) {
    return null;
  }

  if (data.status === "N/A") {
    return <span>N/A</span>;
  }

  return (
    <span title={basicTimeFormat(data.time)}>
      {basicLocalTimeFormat(data.time)}
    </span>
  );
}

export { LastRunAtTime };
