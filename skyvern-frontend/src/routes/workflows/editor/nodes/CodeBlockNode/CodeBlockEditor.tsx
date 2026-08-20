import { ChevronDownIcon, MagicWandIcon } from "@radix-ui/react-icons";
import { useNodes, useReactFlow } from "@xyflow/react";
import { useMemo, useState } from "react";
import type { Extension } from "@uiw/react-codemirror";

import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInputSet } from "@/components/WorkflowBlockInputSet";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { jinjaHighlight } from "@/routes/workflows/components/jinjaHighlight";
import { lineHighlight } from "@/routes/workflows/components/lineHighlight";
import { analyzeCodeBlockErrorCodes } from "@/routes/workflows/editor/codeBlockErrorCodeDiagnostics";
import { ErrorCodeMappingEditor } from "@/routes/workflows/editor/ErrorCodeMappingEditor";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import type { CodeBlockStep } from "@/routes/workflows/types/workflowTypes";
import { getCodeStepPlainText } from "@/routes/workflows/workflowBlockUtils";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { deepEqualStringArrays } from "@/util/equality";
import { cn } from "@/util/utils";

import { type AppNode, isWorkflowBlockNode } from "..";
import { errorMappingExampleValue } from "../types";
import { isStartNode } from "../StartNode/types";
import { CodeBlockPlainCard } from "./CodeBlockPlainCard";
import { getStepLabel } from "./stepPresentation";
import { CodeBlockViewToggle, type CodeBlockView } from "./CodeBlockViewToggle";
import type { CodeBlockNode, CodeBlockNodeData } from "./types";
import { useUpdate } from "../../useUpdate";

function formatStepLines(step: CodeBlockStep): string {
  if (step.line_start == null) {
    return "";
  }
  if (step.line_end == null || step.line_end === step.line_start) {
    return `L${step.line_start}`;
  }
  return `L${step.line_start}-${step.line_end}`;
}

function raisedLineText(lines: Array<number>): string {
  return `raised on ${lines.length === 1 ? "line" : "lines"} ${lines.join(", ")}`;
}

const MAX_RENDERED_ERROR_CODE_DIAGNOSTICS = 50;
const MAX_RENDERED_MALFORMED_LINE_NUMBERS = 20;

function CodeBlockEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "codeBlock") {
    return null;
  }
  return <CodeBlockEditorBody blockId={blockId} node={node as CodeBlockNode} />;
}

function CodeBlockEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: CodeBlockNode;
}) {
  const nodes = useNodes<AppNode>();
  const data = node.data;
  const errorCodeMapping = data.errorCodeMapping ?? "null";
  const { editable } = data;
  const update = useUpdate<CodeBlockNodeData>({ id: blockId, editable });
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const codeFirstAccess = useFeatureFlag("CODE_BLOCK_ACCESS") === true;
  // Code-first layout needs the access flag plus a prompt; otherwise keep the legacy manual layout.
  const isCodeFirst = data.prompt != null && codeFirstAccess;
  const steps = data.steps ?? [];
  const [view, setView] = useState<CodeBlockView>("plain");
  const [stepsOpen, setStepsOpen] = useState(true);
  const [activeStepIndex, setActiveStepIndex] = useState<number | null>(null);
  const activeStep =
    activeStepIndex != null ? (steps[activeStepIndex] ?? null) : null;
  const codeExtensions = useMemo<Array<Extension>>(() => {
    if (activeStep?.line_start == null) {
      return jinjaHighlight;
    }
    const activeLineExtensions = lineHighlight([
      {
        from: activeStep.line_start,
        to: activeStep.line_end ?? activeStep.line_start,
        variant: "active",
      },
    ]);
    return [
      ...jinjaHighlight,
      ...(Array.isArray(activeLineExtensions)
        ? activeLineExtensions
        : [activeLineExtensions]),
    ];
  }, [activeStep?.line_start, activeStep?.line_end]);

  const requestBuild = useCopilotActionStore((state) => state.requestBuild);
  const requestCancel = useCopilotActionStore((state) => state.requestCancel);
  const generatingBlockLabel = useCopilotActionStore(
    (state) => state.generatingBlockLabel,
  );
  const isGenerating =
    generatingBlockLabel != null && generatingBlockLabel === data.label;
  const canGenerate =
    (data.prompt ?? "").trim().length > 0 &&
    !isGenerating &&
    editable &&
    !scopeReadOnly;
  const hasGenerated = steps.length > 0;
  const workflowStartNode = nodes
    .filter(isStartNode)
    .find((candidate) => "errorCodeMapping" in candidate.data);
  const workflowErrorCodeMapping =
    workflowStartNode && "errorCodeMapping" in workflowStartNode.data
      ? workflowStartNode.data.errorCodeMapping
      : null;

  const effectiveManifest = useMemo(() => {
    let blockMapping: Record<string, string> | null = null;
    try {
      const parsed = JSON.parse(errorCodeMapping) as unknown;
      if (
        parsed !== null &&
        typeof parsed === "object" &&
        !Array.isArray(parsed)
      ) {
        blockMapping = parsed as Record<string, string>;
      }
    } catch {
      // The JSON editor owns parse diagnostics; synchronization stays advisory.
    }
    const merged = {
      ...(workflowErrorCodeMapping ?? {}),
      ...(blockMapping ?? {}),
    };
    return Object.keys(merged).length > 0 ? merged : null;
  }, [errorCodeMapping, workflowErrorCodeMapping]);
  const diagnostics = useMemo(
    () => analyzeCodeBlockErrorCodes(data.code, effectiveManifest),
    [data.code, effectiveManifest],
  );

  const goalField = (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs text-tertiary-foreground">Goal</Label>
        <button
          type="button"
          disabled={!canGenerate}
          aria-label={hasGenerated ? "Regenerate block" : "Generate block"}
          onClick={() =>
            requestBuild({ blockLabel: data.label, prompt: data.prompt ?? "" })
          }
          className={cn(
            "nodrag nopan flex items-center gap-1 rounded-md border border-border bg-slate-elevation1 px-2 py-0.5 text-xs text-foreground dark:text-slate-200",
            canGenerate
              ? "hover:bg-slate-elevation2"
              : "cursor-not-allowed opacity-50",
          )}
        >
          <MagicWandIcon className="size-3" />
          {isGenerating
            ? "Generating…"
            : hasGenerated
              ? "Regenerate"
              : "Generate"}
        </button>
      </div>
      <WorkflowBlockInputTextarea
        nodeId={blockId}
        onChange={(value) => update({ prompt: value })}
        value={data.prompt ?? ""}
        className="nopan text-xs"
      />
    </div>
  );

  const codeEditorElement = (
    <CodeEditor
      language="python"
      value={data.code}
      readOnly={scopeReadOnly}
      onChange={(value) => {
        update({ code: value });
      }}
      className="nopan"
      fontSize={10}
      lineWrap={false}
      extraExtensions={codeExtensions}
    />
  );

  const inputsField = (
    <div className="space-y-2">
      <Label className="text-xs text-tertiary-foreground">Inputs</Label>
      <WorkflowBlockInputSet
        nodeId={blockId}
        onChange={(parameterKeys) => {
          const newParameterKeys = Array.from(parameterKeys);
          if (!deepEqualStringArrays(data.parameterKeys, newParameterKeys)) {
            update({ parameterKeys: newParameterKeys });
          }
        }}
        values={new Set(data.parameterKeys ?? [])}
      />
    </div>
  );

  const hasDiagnostics =
    diagnostics.declaredAndRaised.length > 0 ||
    diagnostics.declaredButUnused.length > 0 ||
    diagnostics.raisedButUndeclared.length > 0 ||
    diagnostics.malformedLines.length > 0;
  const diagnosticRows = [
    ...diagnostics.declaredAndRaised.map(({ code, lines }) => ({
      key: `declared-raised-${code}`,
      text: `${code} — ${raisedLineText(lines)}`,
    })),
    ...diagnostics.declaredButUnused.map((code) => ({
      key: `declared-unused-${code}`,
      text: `${code} — declared, not raised`,
    })),
    ...diagnostics.raisedButUndeclared.map(({ code, lines }) => ({
      key: `raised-undeclared-${code}`,
      text: `${code} — ${raisedLineText(lines)}, not declared`,
    })),
    ...(diagnostics.malformedLines.length > 0
      ? [
          {
            key: "malformed",
            text: `Malformed/nonliteral ErrorCode raises (ErrorCode cannot be imported or aliased) — ${
              diagnostics.malformedLines.length === 1 ? "line" : "lines"
            } ${diagnostics.malformedLines
              .slice(0, MAX_RENDERED_MALFORMED_LINE_NUMBERS)
              .join(", ")}${
              diagnostics.malformedLines.length >
              MAX_RENDERED_MALFORMED_LINE_NUMBERS
                ? ` … and ${diagnostics.malformedLines.length - MAX_RENDERED_MALFORMED_LINE_NUMBERS} more`
                : ""
            }`,
          },
        ]
      : []),
  ];
  const hiddenDiagnosticCount = Math.max(
    0,
    diagnosticRows.length - MAX_RENDERED_ERROR_CODE_DIAGNOSTICS,
  );
  const errorCodeMappingField = (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-xs font-normal text-tertiary-foreground">
          Error Messages
        </Label>
        <div className="w-52">
          <Switch
            aria-label="Enable Error Messages"
            checked={errorCodeMapping !== "null"}
            disabled={!editable || scopeReadOnly}
            onCheckedChange={(checked) =>
              update({
                errorCodeMapping: checked
                  ? JSON.stringify(errorMappingExampleValue, null, 2)
                  : "null",
              })
            }
          />
        </div>
      </div>
      {errorCodeMapping !== "null" && (
        <ErrorCodeMappingEditor
          label={data.label}
          value={errorCodeMapping}
          onChange={(value) => update({ errorCodeMapping: value })}
          readOnly={!editable || scopeReadOnly}
        />
      )}
      <p className="text-xs text-muted-foreground">
        The manifest declares public error codes and their conditions. Python
        controls when they fire.
      </p>
      {hasDiagnostics && (
        <div
          aria-label="Error message synchronization status"
          className="rounded border border-border px-2 py-1.5 text-[10px] text-muted-foreground"
        >
          <div className="font-medium text-tertiary-foreground">
            Python synchronization
          </div>
          <ul className="mt-1 space-y-0.5">
            {diagnosticRows
              .slice(0, MAX_RENDERED_ERROR_CODE_DIAGNOSTICS)
              .map(({ key, text }) => (
                <li key={key}>{text}</li>
              ))}
            {hiddenDiagnosticCount > 0 && (
              <li>+{hiddenDiagnosticCount} more</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );

  // Technical step list shown alongside the code: clicking a step highlights
  // the lines it maps to in the editor.
  const stepLineList =
    steps.length > 0 ? (
      <div className="space-y-2">
        <button
          type="button"
          aria-expanded={stepsOpen}
          className="flex w-full items-center justify-between text-xs text-tertiary-foreground"
          onClick={() => setStepsOpen((open) => !open)}
        >
          <span>Steps ({steps.length})</span>
          <ChevronDownIcon
            className={cn(
              "size-4 transition-transform",
              stepsOpen && "rotate-180",
            )}
          />
        </button>
        {stepsOpen && (
          <ol className="space-y-1">
            {steps.map((step, index) => {
              const hasLines = step.line_start != null;
              const isActive = activeStepIndex === index;
              return (
                <li key={index}>
                  <button
                    type="button"
                    disabled={!hasLines}
                    aria-pressed={isActive}
                    onClick={() =>
                      setActiveStepIndex((current) =>
                        current === index ? null : index,
                      )
                    }
                    className={cn(
                      "flex w-full items-center gap-2 rounded bg-slate-elevation1 px-2 py-1 text-left text-xs",
                      hasLines && "hover:bg-slate-elevation2",
                      isActive && "ring-1 ring-sky-400/60",
                      !hasLines && "cursor-default",
                    )}
                  >
                    <span className="w-5 shrink-0 tabular-nums text-muted-foreground dark:text-slate-500">
                      {index + 1}.
                    </span>
                    <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {getStepLabel(step.action_type)}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-tertiary-foreground">
                      {getCodeStepPlainText(step)}
                    </span>
                    {hasLines && (
                      <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                        {formatStepLines(step)}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    ) : null;

  if (!isCodeFirst) {
    return (
      <div data-testid="code-block-block-form" className="space-y-4">
        {inputsField}
        <div className="space-y-2">
          <Label className="text-xs text-tertiary-foreground">Code Input</Label>
          {codeEditorElement}
        </div>
        {errorCodeMappingField}
      </div>
    );
  }

  return (
    <div data-testid="code-block-block-form" className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <Label className="text-xs text-tertiary-foreground">View</Label>
        <CodeBlockViewToggle value={view} onChange={setView} />
      </div>
      {view === "plain" ? (
        <>
          {goalField}
          <CodeBlockPlainCard
            steps={steps}
            generating={isGenerating}
            onStop={requestCancel}
          />
          {errorCodeMappingField}
        </>
      ) : (
        <>
          {stepLineList}
          {inputsField}
          <div className="space-y-2">
            <Label className="text-xs text-tertiary-foreground">
              Code Input
            </Label>
            {codeEditorElement}
          </div>
          {errorCodeMappingField}
        </>
      )}
    </div>
  );
}

export { CodeBlockEditor };
