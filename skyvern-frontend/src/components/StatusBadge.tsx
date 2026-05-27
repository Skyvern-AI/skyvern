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
        "border-green-900/20 bg-green-900/10 text-green-800 hover:bg-green-900/15 dark:border-transparent dark:bg-green-900 dark:text-green-50 dark:hover:bg-green-900/80":
          status === Status.Completed,
        "border-orange-900/20 bg-orange-900/10 text-orange-800 hover:bg-orange-900/15 dark:border-transparent dark:bg-orange-900 dark:text-orange-50 dark:hover:bg-orange-900/80":
          status === Status.Terminated,
        "border-gray-900/20 bg-gray-900/10 text-gray-800 hover:bg-gray-900/15 dark:border-transparent dark:bg-gray-900 dark:text-gray-50 dark:hover:bg-gray-900/80":
          status === Status.Created,
        "border-red-900/20 bg-red-900/10 text-red-800 hover:bg-red-900/15 dark:border-transparent dark:bg-red-900 dark:text-red-50 dark:hover:bg-red-900/80":
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
