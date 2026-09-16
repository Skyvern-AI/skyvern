import { UpdateIcon } from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { basicLocalTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";

type Props = {
  attempt?: number;
  retryPending?: boolean;
  nextAttemptAt?: string | null;
  className?: string;
};

export function WorkflowRunAttemptChip({
  attempt = 1,
  retryPending = false,
  nextAttemptAt,
  className,
}: Props) {
  if (attempt === 1 && !retryPending) return null;
  const label = retryPending ? "Retry pending" : `Attempt ${attempt}`;
  const description = retryPending
    ? `Attempt ${attempt} ended. Next attempt ${nextAttemptAt ? `at ${basicLocalTimeFormat(nextAttemptAt)}` : "soon"}`
    : label;
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            role="img"
            aria-label={description}
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-md text-xs text-blue-400 outline-none focus-visible:ring-2 focus-visible:ring-ring",
              className,
            )}
          >
            <UpdateIcon className="size-3.5" />
            {label}
          </span>
        </TooltipTrigger>
        <TooltipContent>{description}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
