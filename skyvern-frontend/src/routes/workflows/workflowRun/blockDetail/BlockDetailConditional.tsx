import { cn } from "@/util/utils";

import {
  type BranchEvaluation,
  hasEvaluations,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import { JsonExplorer } from "./BlockInspector";
import { Section } from "./shared";

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

/**
 * One branch as a keyword-led rule line: `if` / `else if` / `else` in a muted
 * gutter carries the order, the condition follows in regular type (mono only
 * for a template expression), and the outcome sits at the end — `false` for a
 * condition that did not hold, the destination for the branch that ran. The
 * branch that ran is the one emphasized row; no glyph, no tint.
 */
function BranchRow({
  keyword,
  taken,
  condition,
  template,
  mono = false,
  outcome,
  children,
}: {
  keyword: string;
  taken: boolean;
  condition: string;
  template?: string | null;
  mono?: boolean;
  outcome?:
    | { kind: "next"; label: string }
    | { kind: "result"; value: boolean };
  children?: React.ReactNode;
}) {
  return (
    <li className="space-y-1.5 py-1.5">
      <div
        className={cn(
          "flex items-start gap-3 text-xs",
          taken ? "text-foreground" : "text-muted-foreground",
        )}
      >
        <span className="w-10 shrink-0 font-mono text-muted-foreground/70">
          {keyword}
        </span>
        <span className="min-w-0 flex-1">
          <span className={cn("break-words", mono && "font-mono")}>
            {condition}
          </span>
          {template ? (
            <code className="ml-2 break-all font-mono text-muted-foreground/60">
              {template}
            </code>
          ) : null}
        </span>
        {outcome?.kind === "next" ? (
          <span
            role="img"
            aria-label="taken"
            className="shrink-0 whitespace-nowrap text-muted-foreground"
          >
            →{" "}
            <span className="font-medium text-foreground">{outcome.label}</span>
          </span>
        ) : outcome?.kind === "result" ? (
          <span className="shrink-0 font-mono text-muted-foreground/70">
            {outcome.value ? "true" : "false"}
          </span>
        ) : null}
      </div>
      {children ? <div className="pl-[3.25rem]">{children}</div> : null}
    </li>
  );
}

function keywordFor(index: number, isDefault: boolean): string {
  if (isDefault) return "else";
  return index === 0 ? "if" : "else if";
}

function EvaluationRow({
  evaluation,
  index,
}: {
  evaluation: BranchEvaluation;
  index: number;
}) {
  const keyword = keywordFor(index, evaluation.is_default);
  const outcome = evaluation.is_matched
    ? evaluation.next_block_label
      ? ({ kind: "next", label: evaluation.next_block_label } as const)
      : undefined
    : evaluation.result === null
      ? undefined
      : ({ kind: "result", value: evaluation.result } as const);
  if (evaluation.is_default) {
    return (
      <BranchRow
        keyword={keyword}
        taken={evaluation.is_matched}
        condition="Default branch"
        outcome={outcome}
      />
    );
  }
  const mono = evaluation.criteria_type === "jinja2_template";
  const original = evaluation.original_expression ?? "";
  const rendered =
    evaluation.rendered_expression &&
    evaluation.rendered_expression !== evaluation.original_expression
      ? evaluation.rendered_expression
      : null;
  const renderedJson = rendered ? tryParseJson(rendered) : null;
  // A JSON-shaped rendered value is too wide for the line; it gets the
  // explorer underneath and the template keeps the line.
  if (renderedJson !== null) {
    return (
      <BranchRow
        keyword={keyword}
        taken={evaluation.is_matched}
        condition={original}
        mono={mono}
        outcome={outcome}
      >
        <JsonExplorer value={renderedJson} rootLabel="rendered" />
      </BranchRow>
    );
  }
  return (
    <BranchRow
      keyword={keyword}
      taken={evaluation.is_matched}
      condition={rendered ?? original}
      template={rendered ? original : null}
      mono={mono}
      outcome={outcome}
    />
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
      {hasExecutedBranch && evaluations && evaluations.length > 0 ? (
        <Section title="Branches">
          <ul className="divide-y divide-border/50">
            {evaluations.map((evaluation, index) => (
              <EvaluationRow
                key={evaluation.branch_id || index}
                evaluation={evaluation}
                index={index}
              />
            ))}
          </ul>
        </Section>
      ) : hasExecutedBranch ? (
        <Section title="Evaluation">
          {block.executed_branch_expression ? (
            <ul className="divide-y divide-border/50">
              <BranchRow
                keyword="if"
                taken={Boolean(block.executed_branch_result)}
                condition={block.executed_branch_expression}
                outcome={
                  block.executed_branch_result
                    ? block.executed_branch_next_block
                      ? {
                          kind: "next",
                          label: block.executed_branch_next_block,
                        }
                      : undefined
                    : { kind: "result", value: false }
                }
              />
              {!block.executed_branch_result ? (
                <BranchRow
                  keyword="else"
                  taken
                  condition="Default branch"
                  outcome={
                    block.executed_branch_next_block
                      ? {
                          kind: "next",
                          label: block.executed_branch_next_block,
                        }
                      : undefined
                  }
                />
              ) : null}
            </ul>
          ) : (
            <div className="text-sm text-muted-foreground">
              No conditions matched — executed default branch.
            </div>
          )}
        </Section>
      ) : null}
    </div>
  );
}

export { BlockDetailConditional };
