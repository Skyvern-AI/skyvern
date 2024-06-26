import { Status } from "@/api/types";
import { Badge } from "./ui/badge";

type Props = {
  status: Status;
};

function StatusBadge({ status }: Props) {
  let variant: "default" | "success" | "destructive" | "warning" = "default";
  if (status === "completed") {
    variant = "success";
  } else if (
    status === "failed" ||
    status === "terminated" ||
    status === "timed_out" ||
    status === "canceled"
  ) {
    variant = "destructive";
  } else if (status === "running") {
    variant = "warning";
  }

  const statusText = status === "timed_out" ? "timed out" : status;

  return (
    <Badge className="h-fit" variant={variant}>
      {statusText}
    </Badge>
  );
}

export { StatusBadge };
