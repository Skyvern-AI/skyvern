import type { FailureCategory } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { getFailureCategoryDisplay } from "@/util/failureCategoryDisplay";

type Props = {
  failureCategory: Array<FailureCategory> | null;
};

function FailureCategoryBadge({ failureCategory }: Props) {
  // The HAR diagnostic is appended after the primary, but with no primary it is the only entry.
  const diagnostic = failureCategory?.find(
    (entry) => entry?.reason_code === HAR_ANTI_BOT_DIAGNOSTIC,
  );
  const first = failureCategory?.[0];
  // failure_category is untyped JSON on the backend and can carry model output, so guard at runtime.
  const primary =
    first !== diagnostic &&
    typeof first?.category === "string" &&
    first.category.trim()
      ? getFailureCategoryDisplay(first)
      : null;
  if (!primary && !diagnostic) {
    return null;
  }
  // Self-contained provider so the badge works outside the studio's provider; tabIndex keeps
  // the explanation reachable on keyboard focus, not hover only.
  return (
    <TooltipProvider delayDuration={200}>
      <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-1">
        {primary ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                tabIndex={0}
                className="inline-flex w-fit rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Badge variant="destructive">{primary.label}</Badge>
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-xs">
              {primary.description}
            </TooltipContent>
          </Tooltip>
        ) : null}
        {diagnostic ? <DiagnosticBadge entry={diagnostic} /> : null}
      </span>
    </TooltipProvider>
  );
}

const HAR_ANTI_BOT_DIAGNOSTIC = "har_anti_bot_diagnostic";

function DiagnosticBadge({ entry }: { entry: FailureCategory }) {
  const label =
    typeof entry.error_code === "string" && entry.error_code
      ? entry.error_code
      : "Bot protection seen during the run";
  const contribution =
    typeof entry.anti_bot?.contribution === "string" &&
    entry.anti_bot.contribution
      ? entry.anti_bot.contribution
      : "unknown";
  // An author's error code can be one unbroken token: truncate the badge and let the tooltip
  // carry the full value.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="inline-flex min-w-0 max-w-full rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Badge variant="outline" className="min-w-0 max-w-full">
            <span className="truncate">{label}</span>
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs break-words">
        {typeof entry.reasoning === "string" ? entry.reasoning : label}
        {`\nContribution to this failure: ${contribution}`}
      </TooltipContent>
    </Tooltip>
  );
}

export { FailureCategoryBadge };
