import { ExclamationTriangleIcon } from "@radix-ui/react-icons";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

type Props = {
  outcomeRisk: boolean;
};

// Amber, never red — the run status owns red; an outcome-risk run still
// completed, it just could not verify its recovered outcome.
function RunOutcomeRiskMarker({ outcomeRisk }: Props) {
  if (!outcomeRisk) {
    return null;
  }

  return (
    <TooltipProvider>
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <span
            role="img"
            aria-label="Completed with outcome risk"
            tabIndex={0}
            className="inline-flex items-center text-warning"
          >
            <ExclamationTriangleIcon aria-hidden className="h-4 w-4" />
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px] text-[11px]">
          Completed, but a recovery may not have fully verified the outcome —
          review recommended. Your workflow version is unchanged.
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { RunOutcomeRiskMarker };
