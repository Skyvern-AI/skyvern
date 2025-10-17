import { Status } from "@/api/types";
import { Badge } from "./ui/badge";
import { cn } from "@/util/utils";

type Props = {
  className?: string;
  status: Status | "pending";
};

function StatusBadge({ className, status }: Props) {
  const statusText = status === "timed_out" ? "timed out" : status;

  return (
    <Badge
      className={cn("flex h-7 w-24 justify-center", className, {
        "bg-green-900 text-green-50 hover:bg-green-900/80":
          status === Status.Completed,
        "bg-orange-900 text-orange-50 hover:bg-orange-900/80":
          status === Status.Terminated,
        "bg-gray-900 text-gray-50 hover:bg-gray-900/80":
          status === Status.Created,
        "bg-red-900 text-red-50 hover:bg-red-900/80":
          status === Status.Failed ||
          status === Status.Canceled ||
          status === Status.TimedOut,
        "bg-yellow-900 text-yellow-50 hover:bg-yellow-900/80":
          status === Status.Running ||
          status === Status.Queued ||
          status === "pending",
      })}
    >
      {statusText}
    </Badge>
  );
}

export { StatusBadge };
