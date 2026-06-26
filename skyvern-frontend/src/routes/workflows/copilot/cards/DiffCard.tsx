import { MagicWandIcon } from "@radix-ui/react-icons";

import { TurnNarrativeState } from "../narrativeState";

// eslint-disable-next-line react-refresh/only-export-components
export function shouldShowDiffCard(turn: TurnNarrativeState): boolean {
  if (!turn.draft) {
    return false;
  }
  if (turn.proposalDisposition === "no_proposal") {
    return false;
  }
  return turn.draft.blockLabels.length > 0;
}

export function DiffCard({ turn }: { turn: TurnNarrativeState }) {
  const draft = turn.draft;
  if (!draft) {
    return null;
  }
  const priorLabels = new Set(
    turn.blocks.filter((b) => b.state === "drafted").map((b) => b.label),
  );
  const removed = [...priorLabels].filter(
    (label) => !draft.blockLabels.includes(label),
  );

  return (
    <div className="rounded-lg border border-border bg-slate-elevation2 p-3">
      <div className="flex items-center gap-2 text-xs font-semibold text-studio-accent-2">
        <MagicWandIcon className="h-3.5 w-3.5" />
        {draft.summary ?? "Proposed changes"}
      </div>
      {draft.blockLabels.length > 0 ? (
        <div className="mt-2">
          <div className="text-[10px] font-bold uppercase tracking-wide text-success">
            Added
          </div>
          {draft.blockLabels.map((label) => (
            <div key={label} className="ml-2 text-xs text-foreground">
              + {label}
            </div>
          ))}
        </div>
      ) : null}
      {removed.length > 0 ? (
        <div className="mt-2">
          <div className="text-[10px] font-bold uppercase tracking-wide text-destructive">
            Removed
          </div>
          {removed.map((label) => (
            <div key={label} className="ml-2 text-xs text-foreground">
              - {label}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
