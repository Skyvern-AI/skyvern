import { useMemo } from "react";
import type { Extension } from "@uiw/react-codemirror";

import { ActionsApiResponse, Status } from "@/api/types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { jinjaHighlight } from "@/routes/workflows/components/jinjaHighlight";
import { lineHighlight } from "@/routes/workflows/components/lineHighlight";
import type { CodeBlockUseKind } from "@/routes/workflows/editor/jinjaReferences";
import { actionCodeLine } from "@/routes/workflows/workflowRun/codeBlockFailure";
import {
  GoalText,
  Section,
} from "@/routes/workflows/workflowRun/blockDetail/shared";
import { isCappedValue } from "@/routes/workflows/workflowRun/responseValueCaps";
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
  uses?: Array<CodeBlockRunUse>;
};

type CodeBlockRunUse = {
  name: string;
  kind: CodeBlockUseKind;
  found: boolean;
  value?: unknown;
};

const MAX_VALUE_PREVIEW_CHARS = 2000;

function UseValue({ use }: { use: CodeBlockRunUse }) {
  if (!use.found) {
    return (
      <p className="text-xs text-muted-foreground">
        not available for this run
      </p>
    );
  }
  const text = JSON.stringify(use.value, null, 2) ?? String(use.value);
  return (
    <details className="mt-1 text-xs">
      <summary className="cursor-pointer truncate text-tertiary-foreground">
        {isCappedValue(use.value) ? (
          <span className="mr-1.5 rounded border border-border px-1 py-0.5 text-[10px] text-muted-foreground">
            Capped by the run
          </span>
        ) : null}
        {text.slice(0, 120).replace(/\s+/g, " ")}
      </summary>
      <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-elevation3 p-2 text-[11px] text-tertiary-foreground">
        {text.length > MAX_VALUE_PREVIEW_CHARS
          ? `${text.slice(0, MAX_VALUE_PREVIEW_CHARS)}…`
          : text}
      </pre>
    </details>
  );
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
  uses,
}: Props) {
  // Actions arrive newest-first, so the first failed action carrying a code
  // line is the one that stopped the run.
  const failingAction =
    blockStatus === Status.Failed
      ? (actions ?? []).find(
          (action) =>
            action.status === Status.Failed && actionCodeLine(action) !== null,
        )
      : undefined;
  const failingLine = failingAction ? actionCodeLine(failingAction) : null;
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

  const upstreamUses = (uses ?? []).filter((use) => use.kind === "block");

  return (
    <div className="space-y-4 px-3 py-3">
      <Section title="Code">
        <div className="flex min-w-0 flex-col gap-2">
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
      </Section>
      {prompt ? (
        <Section title="Goal">
          <GoalText text={prompt} />
        </Section>
      ) : null}
      {steps && steps.length > 0 ? (
        <Section title="Steps">
          <ol className="flex min-w-0 flex-col gap-1">
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
                  {step.description}
                </span>
                {step.line_start != null ? (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                    {formatStepLines(step)}
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        </Section>
      ) : null}
      {parameters && parameters.length > 0 ? (
        <Section title="Inputs">
          <div className="flex flex-col gap-3">
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
        </Section>
      ) : null}
      {uses && uses.length > 0 ? (
        <Section title="Uses">
          <ul
            aria-label="Names this code reads"
            className="flex flex-col gap-2"
          >
            {uses.map((use) => (
              <li key={use.name} data-kind={use.kind} className="min-w-0">
                <span className="font-mono text-xs text-foreground dark:text-slate-200">
                  {use.name}
                </span>
                <span className="ml-1.5 text-xs text-muted-foreground">
                  {use.kind}
                </span>
                {use.kind === "input" ? <UseValue use={use} /> : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {upstreamUses.length > 0 ? (
        <Section title="Upstream outputs">
          <ul
            aria-label="Upstream outputs before this execution"
            className="flex flex-col gap-2"
          >
            {upstreamUses.map((use) => (
              <li key={use.name} className="min-w-0">
                <span className="font-mono text-xs text-foreground dark:text-slate-200">
                  {use.name}
                </span>
                <UseValue use={use} />
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

export { CodeBlockParameters, type CodeBlockRunUse };
