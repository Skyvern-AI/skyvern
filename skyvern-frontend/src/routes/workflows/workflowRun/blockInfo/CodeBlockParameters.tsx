import { useMemo } from "react";
import type { Extension } from "@uiw/react-codemirror";

import { ActionsApiResponse, Status } from "@/api/types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { jinjaHighlight } from "@/routes/workflows/components/jinjaHighlight";
import { lineHighlight } from "@/routes/workflows/components/lineHighlight";
import type {
  CodeBlockStep,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

type Props = {
  code: string;
  parameters?: Array<WorkflowParameter>;
  prompt?: string | null;
  steps?: Array<CodeBlockStep> | null;
  blockStatus?: Status | null;
  failureReason?: string | null;
  actions?: Array<ActionsApiResponse> | null;
};

function getActionCodeLine(action: ActionsApiResponse): number | null {
  const output = action.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    return null;
  }
  const codeLine = (output as Record<string, unknown>).code_line;
  return typeof codeLine === "number" ? codeLine : null;
}

function formatStepLines(step: CodeBlockStep): string {
  if (step.line_start == null) {
    return "";
  }
  if (step.line_end == null || step.line_end === step.line_start) {
    return `L${step.line_start}`;
  }
  return `L${step.line_start}-${step.line_end}`;
}

function CodeBlockParameters({
  code,
  parameters,
  prompt,
  steps,
  blockStatus,
  failureReason,
  actions,
}: Props) {
  // Actions arrive newest-first, so the first failed action carrying a code
  // line is the one that stopped the run.
  const failingAction =
    blockStatus === Status.Failed
      ? (actions ?? []).find(
          (action) =>
            action.status === Status.Failed &&
            getActionCodeLine(action) !== null,
        )
      : undefined;
  const failingLine = failingAction ? getActionCodeLine(failingAction) : null;
  const failingReason = failureReason ?? failingAction?.response ?? null;
  const codeExtensions = useMemo<Array<Extension>>(() => {
    if (failingLine == null) {
      return jinjaHighlight;
    }
    const failingLineExtensions = lineHighlight([
      { from: failingLine, to: failingLine, variant: "error" },
    ]);
    return [
      ...jinjaHighlight,
      ...(Array.isArray(failingLineExtensions)
        ? failingLineExtensions
        : [failingLineExtensions]),
    ];
  }, [failingLine]);

  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Code</h1>
          <h2 className="text-base text-muted-foreground">
            The Python snippet executed for this block
          </h2>
        </div>
        <div className="flex w-full min-w-0 flex-col gap-2">
          {failingLine !== null && (
            <div className="rounded border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-xs leading-relaxed text-destructive">
              {`Failed at line ${failingLine}${failingReason ? `: ${failingReason}` : ""}`}
            </div>
          )}
          <CodeEditor
            className="w-full"
            language="python"
            value={code}
            readOnly
            lineWrap={false}
            minHeight="160px"
            maxHeight="400px"
            extraExtensions={codeExtensions}
          />
        </div>
      </div>
      {prompt ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Goal</h1>
            <h2 className="text-base text-muted-foreground">
              What this block is meant to accomplish
            </h2>
          </div>
          <div className="flex w-full min-w-0 items-start text-sm text-foreground dark:text-slate-200">
            {prompt}
          </div>
        </div>
      ) : null}
      {steps && steps.length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Steps</h1>
            <h2 className="text-base text-muted-foreground">
              Plain-language outline of the code
            </h2>
          </div>
          <ol className="flex w-full min-w-0 flex-col gap-1">
            {steps.map((step, index) => (
              <li
                key={index}
                className="flex items-center gap-2 rounded border border-border/40 bg-slate-elevation3 px-2.5 py-1.5 text-xs"
              >
                <span className="w-5 shrink-0 tabular-nums text-muted-foreground dark:text-slate-500">
                  {index + 1}.
                </span>
                <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {step.action_type}
                </span>
                <span className="min-w-0 flex-1 truncate text-tertiary-foreground">
                  {step.title ?? step.description}
                </span>
                {step.line_start != null ? (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                    {formatStepLines(step)}
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {parameters && parameters.length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Inputs</h1>
            <h2 className="text-base text-muted-foreground">
              Inputs passed to this code block
            </h2>
          </div>
          <div className="flex w-full flex-col gap-3">
            {parameters.map((parameter) => (
              <div
                key={parameter.key}
                className="rounded border border-border/40 bg-slate-elevation3 p-3"
              >
                <p className="font-medium">{parameter.key}</p>
                {parameter.description ? (
                  <p className="text-sm text-muted-foreground">
                    {parameter.description}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { CodeBlockParameters };
