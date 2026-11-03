import {
  blockIsBuilding,
  goalActionIsLocked,
  useCopilotActionStore,
} from "@/store/useCopilotActionStore";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

export const secondaryButton =
  "rounded-md border border-border bg-slate-elevation1 px-2 py-0.5 text-xs text-foreground hover:bg-slate-elevation2 disabled:cursor-not-allowed disabled:opacity-50 dark:text-slate-200";
export const primaryButton =
  "rounded-md bg-primary px-2 py-0.5 text-xs text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50";

export function PendingGoalChangesCard() {
  const changes = useCopilotActionStore((state) => state.pendingGoalChanges);
  const codeEditedBlocks = useCopilotActionStore(
    (state) => state.codeEditedBlocks,
  );
  if (changes.length === 0 && codeEditedBlocks.length === 0) {
    return null;
  }
  return (
    <>
      {changes.length === 0 ? null : <GoalChangesSection />}
      {codeEditedBlocks.length === 0 ? null : <CodeEditedSection />}
    </>
  );
}

function useGoalActionLocks() {
  const generatingBlockLabel = useCopilotActionStore(
    (state) => state.generatingBlockLabel,
  );
  const queuedBuilds = useCopilotActionStore((state) => state.queuedBuilds);
  const readOnlyGoalLabels = useCopilotActionStore(
    (state) => state.readOnlyGoalLabels,
  );
  const mutationLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const builds = { generatingBlockLabel, queuedBuilds };
  return {
    isBuilding: (label: string) => blockIsBuilding(builds, label),
    isLocked: (label: string) =>
      goalActionIsLocked(builds, label, {
        readOnly: readOnlyGoalLabels.includes(label),
        mutationLocked,
      }),
  };
}

function CodeEditedSection() {
  const blocks = useCopilotActionStore((state) => state.codeEditedBlocks);
  const suggestingGoalLabels = useCopilotActionStore(
    (state) => state.suggestingGoalLabels,
  );
  const updateGoal = useCopilotActionStore((state) => state.updateGoal);
  const keepGoal = useCopilotActionStore((state) => state.keepGoal);
  const acceptGoal = useCopilotActionStore((state) => state.acceptGoal);
  const { isLocked } = useGoalActionLocks();
  const single = blocks.length === 1;

  return (
    <section
      role="status"
      aria-label="Code changed by hand"
      className="mb-2 space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs"
    >
      <p className="font-medium text-foreground">
        {single
          ? `Code changed on “${blocks[0]!.label}” — Goal may be out of date`
          : `Code changed on ${blocks.length} blocks — Goals may be out of date`}
      </p>
      <ul className="space-y-2">
        {blocks.map((block) => {
          const locked = isLocked(block.label);
          const suggesting = suggestingGoalLabels.includes(block.label);
          return (
            <li key={block.label} className="space-y-1">
              {single ? null : (
                <p className="font-medium text-foreground">{block.label}</p>
              )}
              {block.suggestedGoal === null ? null : (
                <>
                  <p
                    className="line-clamp-3 text-muted-foreground"
                    title={block.goal}
                  >
                    <span aria-hidden="true">− </span>
                    <span className="sr-only">Old Goal: </span>
                    {block.goal}
                  </p>
                  <p
                    className="line-clamp-3 text-foreground"
                    title={block.suggestedGoal}
                  >
                    <span aria-hidden="true">+ </span>
                    <span className="sr-only">Suggested Goal: </span>
                    {block.suggestedGoal}
                  </p>
                </>
              )}
              <div className="flex items-center gap-2">
                {block.suggestedGoal === null ? (
                  <button
                    type="button"
                    aria-label={`Update Goal for ${block.label}`}
                    disabled={locked || suggesting}
                    onClick={() => updateGoal(block.label)}
                    className={primaryButton}
                  >
                    {suggesting ? "Writing a Goal…" : "Update Goal"}
                  </button>
                ) : (
                  <button
                    type="button"
                    aria-label={`Accept the suggested Goal for ${block.label}`}
                    disabled={locked}
                    onClick={() => acceptGoal(block.label)}
                    className={primaryButton}
                  >
                    Accept
                  </button>
                )}
                <button
                  type="button"
                  aria-label={`Keep Goal for ${block.label}`}
                  disabled={locked}
                  onClick={() => keepGoal(block.label)}
                  className={secondaryButton}
                >
                  Keep Goal
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function GoalChangesSection() {
  const changes = useCopilotActionStore((state) => state.pendingGoalChanges);
  const applyPendingGoalChanges = useCopilotActionStore(
    (state) => state.applyPendingGoalChanges,
  );
  const undoGoalChange = useCopilotActionStore((state) => state.undoGoalChange);
  const keepCode = useCopilotActionStore((state) => state.keepCode);
  const { isBuilding, isLocked } = useGoalActionLocks();
  const applying = changes.some((change) => isBuilding(change.label));
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
                <span aria-hidden="true">− </span>
                <span className="sr-only">Old Goal: </span>
                {change.previousGoal}
              </p>
            )}
            <p className="line-clamp-3 text-foreground" title={change.goal}>
              <span aria-hidden="true">+ </span>
              <span className="sr-only">New Goal: </span>
              {change.goal}
            </p>
            {change.codeEditedByHand && !applying ? (
              <p className="text-muted-foreground">
                Its code was also edited by hand. Keep the code if the new Goal
                describes it.
              </p>
            ) : null}
            {applying ? null : (
              <div className="flex items-center gap-2">
                {change.codeEditedByHand ? (
                  <button
                    type="button"
                    aria-label={`Keep my code for ${change.label}`}
                    disabled={isLocked(change.label)}
                    onClick={() => keepCode(change.label)}
                    className={secondaryButton}
                  >
                    Keep my code
                  </button>
                ) : null}
                {change.previousGoal === null ? null : (
                  <button
                    type="button"
                    aria-label={`Undo the Goal change on ${change.label}`}
                    disabled={isLocked(change.label)}
                    onClick={() => undoGoalChange(change.label)}
                    className={secondaryButton}
                  >
                    Undo
                  </button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        {applying
          ? "Applying the new Goal…"
          : single
            ? changes[0]!.codeEditedByHand
              ? "Apply it to rebuild the code from the new Goal."
              : "This block still does what its old Goal said."
            : "These blocks still do what their old Goals said."}
      </p>
      {applying ? null : (
        <button
          type="button"
          onClick={applyPendingGoalChanges}
          className={primaryButton}
        >
          {single ? "Apply new Goal" : "Apply new Goals"}
        </button>
      )}
    </section>
  );
}
