import { Status } from "@/api/types";
import { Badge } from "./ui/badge";

type Props = {
  status: Status;
};

function TaskStatusBadge({ status }: Props) {
  let variant: "default" | "success" | "destructive" | "warning" = "default";
  if (status === "completed") {
    variant = "success";
  } else if (status === "failed" || status === "terminated") {
    variant = "destructive";
  } else if (status === "running") {
    variant = "warning";
  }

  return <Badge variant={variant}>{status}</Badge>;
}

export { TaskStatusBadge };
