import { useWorkflowReliabilityQuery } from "../hooks/useWorkflowReliabilityQuery";
import {
  reliabilityHasActivity,
  reliabilityLabel,
  reliabilityShowsState,
  reliabilityTone,
} from "./reliabilityStatus";

type Props = {
  workflowPermanentId?: string;
};

function formatRate(healRate: number): string {
  return `${Math.round(healRate * 100)}%`;
}

function WorkflowReliabilityPanel({ workflowPermanentId }: Props) {
  const { data: reliability } = useWorkflowReliabilityQuery({
    workflowPermanentId,
  });

  if (!reliability || !reliabilityHasActivity(reliability)) {
    return null;
  }

  const showsState = reliabilityShowsState(reliability);
  const healed = reliability.healed_runs > 0;
  const baseStory = healed
    ? `Self-healed in ${reliability.healed_runs} of the last ${reliability.window_runs} runs`
    : `Fell back to a backup on ${reliability.floor_runs} of the last ${reliability.window_runs} runs`;
  const streakStory =
    healed && reliability.consecutive_healed_runs > 1
      ? ` - ${reliability.consecutive_healed_runs} in a row`
      : "";
  const rateStory = healed
    ? ` - ${formatRate(reliability.heal_rate)} rate`
    : "";

  if (!showsState) {
    return (
      <section className="rounded-md border border-border bg-slate-elevation1/80 px-3 py-2">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Reliability
        </p>
        <p className="mt-1 text-sm text-muted-foreground">
          {baseStory} - not enough runs to assess yet.
        </p>
      </section>
    );
  }

  const isHealthy = reliability.state === "healthy";
  const tone = reliabilityTone(reliability.state);
  const isAmber = tone === "amber";

  return (
    <section className="rounded-md border border-border bg-slate-elevation1/80 px-3 py-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Reliability
      </p>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        {!isHealthy ? (
          <span className="inline-flex items-center rounded border border-border bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
            {reliabilityLabel(reliability.state)}
          </span>
        ) : null}
        <p
          className={
            isAmber ? "text-sm text-warning" : "text-sm text-muted-foreground"
          }
        >
          {baseStory}
          {streakStory}
          {rateStory}
        </p>
      </div>
    </section>
  );
}

export { WorkflowReliabilityPanel };
