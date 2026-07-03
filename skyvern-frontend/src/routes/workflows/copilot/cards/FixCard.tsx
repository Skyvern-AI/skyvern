import { ExclamationTriangleIcon } from "@radix-ui/react-icons";

import { TurnNarrativeState } from "../narrativeState";

// eslint-disable-next-line react-refresh/only-export-components
export function shouldShowFixCard(turn: TurnNarrativeState): boolean {
  return (
    turn.terminal === "error" || turn.blocks.some((b) => b.state === "failed")
  );
}

function failedBlockError(turn: TurnNarrativeState): string | null {
  const failed = turn.blocks.find((b) => b.state === "failed");
  if (!failed) {
    return null;
  }
  const lastResult = [...failed.activity]
    .reverse()
    .find((e) => e.kind === "tool_result");
  return (
    lastResult?.text ??
    failed.activity[failed.activity.length - 1]?.text ??
    null
  );
}

type FixCardProps = {
  turn: TurnNarrativeState;
  onFix: () => void;
  onExplain?: () => void;
};

export function FixCard({ turn, onFix, onExplain }: FixCardProps) {
  const headline =
    turn.terminalMessage ??
    failedBlockError(turn) ??
    "The last run hit an error.";

  return (
    <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3">
      <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
        <ExclamationTriangleIcon className="h-4 w-4 text-destructive" />
        Run failed — here's a fix
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{headline}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onFix}
          className="rounded-md bg-cta px-3 py-1 text-xs font-medium text-cta-foreground hover:bg-cta-hover"
        >
          Fix with Copilot
        </button>
        {onExplain ? (
          <button
            type="button"
            onClick={onExplain}
            className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          >
            Explain
          </button>
        ) : null}
      </div>
    </div>
  );
}
