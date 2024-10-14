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
    status === "timed_out" ||
    status === "canceled"
  ) {
    variant = "destructive";
  } else if (status === "running" || status === "terminated") {
    variant = "warning";
  }

  const statusText = status === "timed_out" ? "timed out" : status;

  return (
    <Badge className="flex h-7 w-24 justify-center" variant={variant}>
      {statusText}
    </Badge>
  );
}

export { StatusBadge };
