import {
  hasEvaluations,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import { JsonExplorer } from "./BlockInspector";
import { BlockDetailFailure, CodeBlock, Section } from "./shared";
import { cn } from "@/util/utils";

type Props = {
  block: WorkflowRunBlock;
};

function tryParseJson(value: string): unknown | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

function RenderedExpression({ value }: { value: string }) {
  const parsedJson = tryParseJson(value);
  if (parsedJson !== null) {
    return <JsonExplorer value={parsedJson} rootLabel="rendered" />;
  }
  return (
    <code className="break-all rounded bg-slate-elevation1 px-1.5 py-0.5 font-mono text-slate-200">
      {value}
    </code>
  );
}

function BlockDetailConditional({ block }: Props) {
  const evaluations =
    hasEvaluations(block.output) && block.output.evaluations
      ? block.output.evaluations
      : null;
  // Gate evaluation/branch rendering on the conditional having actually
  // resolved a branch. Before that (Created/Queued/Running), claiming a
  // result — especially the "executed default branch" fallback — is wrong.
  const hasExecutedBranch = Boolean(block.executed_branch_id);

  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      {hasExecutedBranch && evaluations && evaluations.length > 0 ? (
        <Section title="Branches">
          <div className="space-y-2">
            {evaluations.map((evaluation, index) => (
              <div
                key={evaluation.branch_id || index}
                className={cn(
                  "space-y-1.5 rounded border px-2.5 py-2 text-xs",
                  evaluation.is_matched
                    ? "border-success/50 bg-success/10"
                    : "border-slate-600 bg-slate-elevation3",
                )}
              >
                {evaluation.is_default ? (
                  <div className="text-slate-300">
                    <span className="font-medium">Default branch</span>
                    {evaluation.is_matched && (
                      <span className="ml-2 text-success">✓ Matched</span>
                    )}
                  </div>
                ) : (
                  <div className="space-y-1.5">
                    <div className="text-slate-400">
                      <span className="text-[10px] uppercase tracking-wide text-slate-500">
                        Expression
                      </span>
                      <div className="mt-0.5">
                        <code className="break-all rounded bg-slate-elevation1 px-1.5 py-0.5 font-mono text-slate-200">
                          {evaluation.original_expression}
                        </code>
                      </div>
                    </div>
                    {evaluation.rendered_expression &&
                      evaluation.rendered_expression !==
                        evaluation.original_expression && (
                        <div className="text-slate-400">
                          <span className="text-[10px] uppercase tracking-wide text-slate-500">
                            Rendered
                          </span>
                          <div className="mt-0.5">
                            <RenderedExpression
                              value={evaluation.rendered_expression}
                            />
                          </div>
                        </div>
                      )}
                    <div className="flex items-center gap-2">
                      <span className="text-slate-500">Result:</span>
                      <span
                        className={cn(
                          "font-medium",
                          evaluation.result ? "text-success" : "text-red-400",
                        )}
                      >
                        {evaluation.result ? "True" : "False"}
                      </span>
                      {evaluation.is_matched && (
                        <span className="text-success">✓ Matched</span>
                      )}
                    </div>
                  </div>
                )}
                {evaluation.is_matched && evaluation.next_block_label && (
                  <div className="border-t border-slate-600 pt-1.5 text-slate-400">
                    → Next:{" "}
                    <span className="font-medium text-slate-200">
                      {evaluation.next_block_label}
                    </span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      ) : hasExecutedBranch ? (
        <Section title="Evaluation">
          {block.executed_branch_expression ? (
            <div className="space-y-1.5 text-sm text-slate-300">
              <div>
                <span className="text-slate-500">Expression: </span>
                <CodeBlock className="mt-1">
                  {block.executed_branch_expression}
                </CodeBlock>
              </div>
              <div>
                <span className="text-slate-500">Result: </span>
                <span
                  className={cn(
                    "font-medium",
                    block.executed_branch_result
                      ? "text-success"
                      : "text-red-400",
                  )}
                >
                  {block.executed_branch_result ? "True" : "False"}
                </span>
              </div>
            </div>
          ) : (
            <div className="text-sm text-slate-400">
              No conditions matched — executed default branch.
            </div>
          )}
        </Section>
      ) : null}
    </div>
  );
}

export { BlockDetailConditional };
