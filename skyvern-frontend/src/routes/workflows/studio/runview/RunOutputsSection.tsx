import { DownloadIcon, FileIcon } from "@radix-ui/react-icons";

import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { SummarizeOutput } from "@/components/SummarizeOutput";

import { outputFieldEntries } from "../runProjections";
import { OverviewCodeBlock } from "./OverviewCodeBlock";
import { OverviewField } from "./OverviewField";
import { RunFieldValue } from "./RunFieldValue";

export type RunOutputFile = { url: string; filename: string };
export type RunOutputError = Record<string, unknown>;

type RunOutputsSectionProps = {
  workflowRunId: string;
  workflowTitle?: string | null;
  outputs: Record<string, unknown> | null;
  extractedInformation: Record<string, unknown> | null;
  files: RunOutputFile[];
  errors: RunOutputError[];
  // Task 2.0 runs report their output on task_v2, not the run-level outputs.
  observerOutput?: Record<string, unknown> | null;
  webhookFailureReason?: string | null;
  // Owned by RunView so the generated summary survives center-tab switches
  // (this section unmounts when another tab takes the center).
  summary: string | null;
  onSummary: (summary: string | null) => void;
};

function readStringField(
  record: Record<string, unknown>,
  keys: Array<string>,
): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value !== "string") {
      continue;
    }
    const trimmed = value.trim();
    if (trimmed !== "") {
      return trimmed;
    }
  }
  return null;
}

function getErrorCode(error: RunOutputError): string | null {
  return readStringField(error, ["error_code", "code"]);
}

function getErrorMessage(error: RunOutputError): string | null {
  return readStringField(error, [
    "reasoning",
    "message",
    "detail",
    "error",
    "error_message",
    "description",
  ]);
}

type RunErrorRow = { code: string | null; message: string | null };

// One row per distinct (code, message); a code-only entry stays a row so a
// failure with no prose still shows its code.
function getErrorRows(errors: RunOutputError[]): RunErrorRow[] {
  const seen = new Set<string>();
  const rows: RunErrorRow[] = [];
  for (const error of errors) {
    const code = getErrorCode(error);
    const message = getErrorMessage(error);
    if (!code && !message) {
      continue;
    }
    const key = `${code ?? ""}\u0000${message ?? ""}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    rows.push({ code, message });
  }
  return rows;
}

function hasRenderableErrors(errors: RunOutputError[]): boolean {
  return getErrorRows(errors).length > 0;
}

/**
 * The run's errors as a field among the Outputs fields — same label grammar
 * as "Extracted information" and "Run outputs", one row per error, the code
 * once. The Timeline view's failure alert is where the failure gets a card
 * and its actions; this view is the data.
 */
function RunErrorsField({ errors }: { errors: RunOutputError[] }) {
  const rows = getErrorRows(errors);
  if (rows.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-medium text-muted-foreground">
        Errors
        <span className="ml-1.5 tabular-nums text-muted-foreground/70">
          {rows.length}
        </span>
      </span>
      <ul className="divide-y divide-border/50">
        {rows.map((row, index) => (
          <li
            key={`${row.code ?? "error"}-${index}`}
            className="flex items-start gap-3 py-1.5 text-sm text-foreground"
          >
            {row.code ? (
              <code className="shrink-0 font-mono text-[11px] leading-5 text-destructive">
                {row.code}
              </code>
            ) : null}
            {row.message ? (
              <span className="min-w-0 whitespace-pre-wrap break-words">
                {row.message}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RunOutputsSection({
  workflowRunId,
  workflowTitle,
  outputs,
  extractedInformation,
  files,
  errors,
  observerOutput = null,
  webhookFailureReason = null,
  summary,
  onSummary,
}: RunOutputsSectionProps) {
  const hasExtracted =
    extractedInformation != null &&
    Object.values(extractedInformation).some((value) => value !== null);
  const hasErrors = hasRenderableErrors(errors);
  // extracted_information renders in its own section, so the remaining keys are the
  // per-block returned values. Gate the run-outputs block on real per-field content
  // or a persisted summary — an empty header + Summarize would render over nothing.
  const outputFields = outputFieldEntries(outputs);
  const hasAgentRunOutputs = outputFields.length > 0 || summary !== null;
  const hasAnyOutput =
    hasExtracted ||
    files.length > 0 ||
    hasErrors ||
    observerOutput != null ||
    Boolean(webhookFailureReason) ||
    hasAgentRunOutputs;
  if (!hasAnyOutput) {
    return null;
  }

  return (
    <div className="flex flex-col gap-5">
      <RunErrorsField errors={errors} />
      {webhookFailureReason ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Webhook failure reason
          </span>
          <div className="whitespace-pre-wrap rounded-md border border-border bg-slate-elevation3 p-3 text-sm text-warning">
            {webhookFailureReason}
          </div>
        </div>
      ) : null}
      {observerOutput != null ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Task 2.0 output
          </span>
          <OverviewCodeBlock
            value={JSON.stringify(observerOutput, null, 2)}
            maxHeight="320px"
          />
        </div>
      ) : null}
      {hasExtracted ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Extracted information
          </span>
          <OverviewCodeBlock
            value={JSON.stringify(extractedInformation, null, 2)}
            maxHeight="320px"
          />
        </div>
      ) : null}
      {hasAgentRunOutputs ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              Run outputs
            </span>
            <SummarizeOutput
              key={`run:${workflowRunId}`}
              contextKey={`run:${workflowRunId}`}
              outputJson={JSON.stringify(outputs)}
              workflowTitle={workflowTitle}
              hasSummary={summary !== null}
              onSummary={onSummary}
            />
          </div>
          {summary !== null ? (
            <div
              role="status"
              aria-live="polite"
              className="whitespace-pre-wrap rounded-md border border-border bg-slate-elevation3 p-3 text-sm"
            >
              {summary}
            </div>
          ) : null}
          {outputFields.length > 0 ? (
            <div className="flex flex-col gap-4">
              {outputFields.map(([key, value]) => (
                <OverviewField key={key} label={key}>
                  <RunFieldValue value={value} label={key} />
                </OverviewField>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {files.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Downloaded files
          </span>
          <div className="flex flex-col gap-1">
            {files.map((file) => (
              <ArtifactDownloadLink
                key={file.url}
                href={file.url}
                title={file.url}
                aria-label={`Download ${file.filename}`}
                className="group flex items-center gap-2.5 rounded-md border border-border bg-slate-elevation2 px-3 py-2 text-sm text-foreground transition-colors hover:bg-slate-elevation3"
              >
                <FileIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{file.filename}</span>
                <DownloadIcon className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" />
              </ArtifactDownloadLink>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
