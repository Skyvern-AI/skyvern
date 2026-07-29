import { Link } from "react-router-dom";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { JsonView, Section } from "./shared";

type Props = {
  block: WorkflowRunBlock;
};

type SubWorkflowRun = {
  workflow_run_id?: string;
  workflow_permanent_id?: string;
  status?: string;
  outputs?: unknown;
};

function asSubWorkflowRun(value: unknown): SubWorkflowRun | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as SubWorkflowRun;
}

function BlockDetailWorkflowTrigger({ block }: Props) {
  const subRun = asSubWorkflowRun(block.output);
  const hasSubRunOutput =
    subRun !== null &&
    subRun.outputs !== undefined &&
    subRun.outputs !== null &&
    !(
      typeof subRun.outputs === "object" &&
      !Array.isArray(subRun.outputs) &&
      Object.keys(subRun.outputs as object).length === 0
    );

  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      {subRun?.workflow_run_id && (
        <Section title="Triggered run">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Link
              to={`/runs/${subRun.workflow_run_id}`}
              className="break-all font-mono text-sky-700 hover:underline dark:text-sky-400"
            >
              {subRun.workflow_run_id}
            </Link>
            {subRun.status && (
              <span className="text-muted-foreground dark:text-slate-500">
                ·{" "}
                <span className="text-tertiary-foreground">
                  {subRun.status}
                </span>
              </span>
            )}
          </div>
        </Section>
      )}
      {hasSubRunOutput && (
        <Section title="Sub-workflow output">
          <JsonView value={subRun!.outputs} />
        </Section>
      )}
    </div>
  );
}

export { BlockDetailWorkflowTrigger };
