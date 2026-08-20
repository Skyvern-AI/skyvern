import { TurnNarrativeState } from "../narrativeState";

// Backend source: skyvern/forge/sdk/copilot/agent.py's _with_inline_reject_note
// append when a DIAGNOSE-authority turn's inline REPLACE_WORKFLOW is downgraded
// to a REPLY ("...confirm and I'll apply the change."). This is the only place
// that phrase family is emitted server-side today — no typed signal exists yet,
// so this is a conservative text match, not a structural one. If the backend
// wording changes, grep for this constant.
export const CONFIRM_REQUEST_PATTERN = /confirm and i(?:'|’)ll apply/i;

// eslint-disable-next-line react-refresh/only-export-components
export function shouldShowConfirmCard(turn: TurnNarrativeState): boolean {
  // The note is only ever appended on the REPLY path (terminal "response"),
  // never "error" — no reason for an error terminal to earn this affordance.
  if (turn.terminal !== "response") {
    return false;
  }
  const text = `${turn.terminalMessage ?? ""} ${turn.narrativeSummary ?? ""}`;
  return CONFIRM_REQUEST_PATTERN.test(text);
}

type ConfirmCardProps = {
  onConfirm: () => void;
  onChangeInstead: () => void;
};

export function ConfirmCard({ onConfirm, onChangeInstead }: ConfirmCardProps) {
  return (
    <div className="flex flex-wrap gap-2 pl-1">
      <button
        type="button"
        onClick={onConfirm}
        className="rounded-md bg-success px-3 py-1.5 text-xs font-semibold text-success-foreground hover:opacity-90"
      >
        Confirm
      </button>
      <button
        type="button"
        onClick={onChangeInstead}
        className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-slate-elevation4 hover:text-foreground dark:hover:text-slate-200"
      >
        Tell it what to change instead
      </button>
    </div>
  );
}
