import { Cross2Icon, ReloadIcon } from "@radix-ui/react-icons";

import { Label } from "@/components/ui/label";
import type { CodeBlockStep } from "@/routes/workflows/types/workflowTypes";
import { getCodeStepPlainText } from "@/routes/workflows/workflowBlockUtils";
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
    <div data-testid="code-block-plain-card" className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label className="text-xs text-slate-300">Steps</Label>
        {generating ? (
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
        ) : (
          steps.length > 0 && (
            <span className="shrink-0 text-xs tabular-nums text-slate-400">
              {steps.length} {steps.length === 1 ? "step" : "steps"}
            </span>
          )
        )}
      </div>
      {generating ? (
        <ol className="space-y-1.5" aria-busy="true">
          {[0, 1, 2].map((index) => (
            <li
              key={index}
              className="flex items-center gap-2.5 rounded-md border border-slate-700/60 bg-slate-elevation1 px-2.5 py-2"
            >
              <div className="size-7 shrink-0 animate-pulse rounded-md bg-slate-elevation2" />
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="h-3 w-2/3 animate-pulse rounded bg-slate-elevation2" />
                <div className="h-2.5 w-1/3 animate-pulse rounded bg-slate-elevation2" />
              </div>
            </li>
          ))}
        </ol>
      ) : steps.length === 0 ? (
        <p className="rounded-md border border-dashed border-slate-700/70 px-3 py-4 text-xs text-slate-400">
          No steps yet. Steps appear once Skyvern generates them.
        </p>
      ) : (
        <ol className="space-y-1.5">
          {steps.map((step, index) => {
            const plainText = getCodeStepPlainText(step);
            return (
              <li
                key={index}
                className="flex items-start gap-2.5 rounded-md border border-slate-700/60 bg-slate-elevation1 px-2.5 py-2"
              >
                <div
                  className={cn(
                    "flex size-7 shrink-0 items-center justify-center rounded-md",
                    getStepChipClassName(step.action_type),
                  )}
                >
                  {getStepIcon(step.action_type)}
                </div>
                <div className="min-w-0 flex-1">
                  <div
                    className="truncate text-xs font-medium text-slate-100"
                    title={plainText}
                  >
                    {plainText}
                  </div>
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {getStepLabel(step.action_type)}
                  </div>
                </div>
                <span className="shrink-0 text-[11px] tabular-nums text-slate-500">
                  {index + 1}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

export { CodeBlockPlainCard };
