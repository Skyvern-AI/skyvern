import { Cross2Icon, ReloadIcon } from "@radix-ui/react-icons";

import type { CodeBlockStep } from "@/routes/workflows/types/workflowTypes";
import { cn } from "@/util/utils";

import {
  getStepChipClassName,
  getStepIcon,
  getStepLabel,
} from "./stepPresentation";

type Props = {
  steps: Array<CodeBlockStep>;
  generating?: boolean;
  onStop?: () => void;
};

function CodeBlockPlainCard({ steps, generating = false, onStop }: Props) {
  return (
    <div data-testid="code-block-plain-card" className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-semibold text-slate-200">
          What Skyvern will do
        </div>
        {generating && (
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <span className="flex items-center gap-1">
              <ReloadIcon className="size-3 animate-spin" />
              Generating…
            </span>
            {onStop && (
              <button
                type="button"
                onClick={onStop}
                className="nodrag nopan flex items-center gap-1 rounded border border-slate-700 px-1.5 py-0.5 text-slate-300 hover:bg-slate-elevation2"
              >
                <Cross2Icon className="size-3" />
                Stop
              </button>
            )}
          </div>
        )}
      </div>
      {generating ? (
        <ol className="space-y-0" aria-busy="true">
          {[0, 1, 2].map((index) => (
            <li key={index} className="flex gap-3">
              <div className="flex flex-col items-center">
                <div className="size-8 shrink-0 animate-pulse rounded-lg bg-slate-elevation2" />
                {index < 2 && <div className="w-px flex-1 bg-slate-700/60" />}
              </div>
              <div className="min-w-0 flex-1 pb-4">
                <div className="h-3.5 w-2/3 animate-pulse rounded bg-slate-elevation2" />
                <div className="mt-1.5 h-2.5 w-1/3 animate-pulse rounded bg-slate-elevation2" />
              </div>
            </li>
          ))}
        </ol>
      ) : steps.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-700/70 px-3 py-4 text-xs text-slate-400">
          No steps yet. Steps appear once Skyvern generates them.
        </p>
      ) : (
        <ol>
          {steps.map((step, index) => {
            const isLast = index === steps.length - 1;
            return (
              <li key={index} className="flex gap-3">
                <div className="flex flex-col items-center">
                  <div
                    className={cn(
                      "flex size-8 shrink-0 items-center justify-center rounded-lg",
                      getStepChipClassName(step.action_type),
                    )}
                  >
                    {getStepIcon(step.action_type)}
                  </div>
                  {!isLast && <div className="w-px flex-1 bg-slate-700/60" />}
                </div>
                <div className={cn("min-w-0 flex-1", !isLast && "pb-4")}>
                  <div className="flex items-baseline gap-2">
                    <span className="shrink-0 text-xs tabular-nums text-slate-500">
                      {index + 1}
                    </span>
                    <span className="text-sm font-medium text-slate-100">
                      {step.title ?? step.description ?? ""}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-slate-400">
                    {getStepLabel(step.action_type)}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

export { CodeBlockPlainCard };
