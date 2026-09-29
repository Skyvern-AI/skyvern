import { useCopilotActionStore } from "@/store/useCopilotActionStore";

export function PendingGoalChangesCard() {
  const changes = useCopilotActionStore((state) => state.pendingGoalChanges);
  const generatingBlockLabel = useCopilotActionStore(
    (state) => state.generatingBlockLabel,
  );
  const queuedBuilds = useCopilotActionStore((state) => state.queuedBuilds);
  const applyPendingGoalChanges = useCopilotActionStore(
    (state) => state.applyPendingGoalChanges,
  );
  const undoGoalChange = useCopilotActionStore((state) => state.undoGoalChange);

  if (changes.length === 0) {
    return null;
  }
  const applying = changes.some(
    (change) =>
      change.label === generatingBlockLabel ||
      queuedBuilds.some((queued) => queued.blockLabel === change.label),
  );
  const single = changes.length === 1;

  return (
    <section
      aria-label="Goal changes not applied"
      className="mb-2 space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs"
    >
      <p className="font-medium text-foreground">
        {single
          ? `Goal changed on “${changes[0]!.label}”`
          : `Goals changed on ${changes.length} blocks`}
      </p>
      <ul className="space-y-2">
        {changes.map((change) => (
          <li key={change.label} className="space-y-1">
            {single ? null : (
              <p className="font-medium text-foreground">{change.label}</p>
            )}
            {change.previousGoal === null ? null : (
              <p
                className="line-clamp-3 text-muted-foreground"
                title={change.previousGoal}
              >
                <span aria-label="Old Goal">− </span>
                {change.previousGoal}
              </p>
            )}
            <p className="line-clamp-3 text-foreground" title={change.goal}>
              <span aria-label="New Goal">+ </span>
              {change.goal}
            </p>
            {change.previousGoal === null || applying ? null : (
              <button
                type="button"
                aria-label={`Undo the Goal change on ${change.label}`}
                onClick={() => undoGoalChange(change.label)}
                className="rounded-md border border-border bg-slate-elevation1 px-2 py-0.5 text-xs text-foreground hover:bg-slate-elevation2 dark:text-slate-200"
              >
                Undo
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        {applying
          ? "Applying the new Goal…"
          : single
            ? "This block still does what its old Goal said."
            : "These blocks still do what their old Goals said."}
      </p>
      {applying ? null : (
        <button
          type="button"
          onClick={applyPendingGoalChanges}
          className="rounded-md bg-primary px-2 py-0.5 text-xs text-primary-foreground hover:bg-primary/90"
        >
          {single ? "Apply new Goal" : "Apply new Goals"}
        </button>
      )}
    </section>
  );
}
