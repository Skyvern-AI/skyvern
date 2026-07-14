import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { stringifyTimelineValue } from "./formatValue";
import { BlockDetailFailure, CodeBlock, Section } from "./shared";

type Props = {
  block: WorkflowRunBlock;
  iterationIndex?: number | null;
};

function BlockDetailLoop({ block, iterationIndex = null }: Props) {
  const loopValues = Array.isArray(block.loop_values) ? block.loop_values : [];
  const isForLoop = block.block_type === "for_loop";
  const highlightIndex =
    iterationIndex !== null && iterationIndex >= 0 ? iterationIndex : null;
  const hasResolvedForLoopIteration =
    isForLoop && highlightIndex !== null && highlightIndex < loopValues.length;
  const highlightedValue = hasResolvedForLoopIteration
    ? loopValues[highlightIndex]
    : null;

  // Show the iteration-only view only when we can actually resolve a value
  // for the selected iteration. `null` is a valid loop value, so gate on
  // index resolvability rather than truthiness.
  const showIterationOnly = hasResolvedForLoopIteration;
  const showSelectedWhileLoopIteration = !isForLoop && highlightIndex !== null;
  const displayedIteration = showSelectedWhileLoopIteration
    ? highlightIndex
    : block.current_index;

  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      {showIterationOnly && (
        <Section title={`Iteration ${(highlightIndex ?? 0) + 1} value`}>
          <CodeBlock>{stringifyTimelineValue(highlightedValue)}</CodeBlock>
        </Section>
      )}
      {!showIterationOnly && isForLoop && (
        <Section title={`Iterable values (${loopValues.length})`}>
          {loopValues.length > 0 ? (
            <div className="space-y-1.5">
              {loopValues.map((value, index) => {
                const full = stringifyTimelineValue(value);
                return (
                  <div
                    key={index}
                    className="flex min-w-0 gap-2 text-xs text-tertiary-foreground"
                  >
                    <span className="shrink-0 self-start py-2 text-muted-foreground dark:text-slate-500">
                      [{index}]
                    </span>
                    <CodeBlock className="min-w-0 flex-1">{full}</CodeBlock>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground dark:text-slate-500">
              No values.
            </div>
          )}
        </Section>
      )}
      {!showIterationOnly && displayedIteration !== null && (
        <Section
          title={
            showSelectedWhileLoopIteration
              ? "Selected iteration"
              : "Current iteration"
          }
        >
          <span className="text-sm text-foreground dark:text-slate-200">
            {displayedIteration + 1}
            {isForLoop && loopValues.length > 0
              ? ` of ${loopValues.length}`
              : ""}
          </span>
        </Section>
      )}
    </div>
  );
}

export { BlockDetailLoop };
