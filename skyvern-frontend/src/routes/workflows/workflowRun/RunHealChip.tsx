import { useRunHealEpisodesQuery } from "../hooks/useRunHealEpisodesQuery";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { healChipTooltip } from "./healStatus";

type Props = {
  workflowRunId?: string;
};

function RunHealChip({ workflowRunId }: Props) {
  const { data } = useRunHealEpisodesQuery({ workflowRunId });
  const summary = data?.summary;

  if (!summary || summary.blocks_with_heal_attempt <= 0) {
    return null;
  }

  const hasOutcomeRisk = summary.blocks_outcome_risk.length > 0;
  const healed = summary.blocks_healed > 0;

  return (
    <TooltipProvider>
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <div className="inline-flex items-center gap-2 rounded-md border border-border bg-slate-elevation2/80 px-2 py-1 text-[11px] text-foreground backdrop-blur-sm">
            <span>
              {healed
                ? `${summary.blocks_healed} block(s) self-healed`
                : "Self-heal attempted"}
            </span>
            {hasOutcomeRisk && (
              <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
                review recommended
              </span>
            )}
          </div>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px] text-[11px]">
          {healChipTooltip(healed)}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { RunHealChip };
