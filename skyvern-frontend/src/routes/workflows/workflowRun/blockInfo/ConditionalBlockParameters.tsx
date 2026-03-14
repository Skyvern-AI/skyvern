import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type { BranchCondition } from "@/routes/workflows/types/workflowTypes";

type Props = {
  branchConditions: Array<BranchCondition> | null;
  executedBranchId: string | null;
  executedBranchExpression: string | null;
  executedBranchResult: boolean | null;
  executedBranchNextBlock: string | null;
};

function ConditionalBlockParameters({
  branchConditions,
  executedBranchId,
  executedBranchExpression,
  executedBranchResult,
  executedBranchNextBlock,
}: Props) {
  return (
    <div className="space-y-4">
      {executedBranchExpression ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Executed Expression</h1>
            <h2 className="text-base text-slate-400">
              The branch expression that was evaluated
            </h2>
          </div>
          <AutoResizingTextarea value={executedBranchExpression} readOnly />
        </div>
      ) : null}
      {typeof executedBranchResult === "boolean" ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Branch Result</h1>
          </div>
          <div className="flex w-full items-center gap-3">
            <Switch checked={executedBranchResult} disabled />
            <span className="text-sm text-slate-400">
              {executedBranchResult ? "True" : "False"}
            </span>
          </div>
        </div>
      ) : null}
      {executedBranchNextBlock ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Next Block</h1>
            <h2 className="text-base text-slate-400">
              The block that was executed after the condition
            </h2>
          </div>
          <Input value={executedBranchNextBlock} readOnly />
        </div>
      ) : null}
      {executedBranchId ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Executed Branch ID</h1>
          </div>
          <Input value={executedBranchId} readOnly />
        </div>
      ) : null}
      {branchConditions && branchConditions.length > 0 ? (
        <div className="space-y-3">
          <h2 className="text-base font-semibold text-slate-300">
            Branch Conditions
          </h2>
          {branchConditions.map((condition) => (
            <div
              key={condition.id}
              className="space-y-2 rounded border border-slate-700/40 bg-slate-elevation3 p-3"
            >
              {condition.description ? (
                <p className="text-sm text-slate-400">
                  {condition.description}
                </p>
              ) : null}
              {condition.criteria?.expression ? (
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Expression:</span>
                  <code className="text-sm text-slate-300">
                    {condition.criteria.expression}
                  </code>
                </div>
              ) : null}
              {condition.next_block_label ? (
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Next Block:</span>
                  <span className="text-sm text-slate-300">
                    {condition.next_block_label}
                  </span>
                </div>
              ) : null}
              {condition.is_default ? (
                <span className="inline-block rounded bg-slate-700 px-2 py-0.5 text-xs">
                  Default
                </span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export { ConditionalBlockParameters };
