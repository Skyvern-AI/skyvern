import {
  DownloadIcon,
  ExclamationTriangleIcon,
  FileIcon,
} from "@radix-ui/react-icons";

import { SummarizeOutput } from "@/components/SummarizeOutput";

import { OverviewCodeBlock } from "./OverviewCodeBlock";

export type RunOutputFile = { url: string; filename: string };
export type RunOutputError = Record<string, unknown>;

type RunOutputsSectionProps = {
  workflowRunId: string;
  workflowTitle?: string | null;
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

function uniqueNonEmpty(
  values: Array<string | null | undefined>,
): Array<string> {
  const seen = new Set<string>();
  const result: Array<string> = [];
  for (const value of values) {
    const trimmed = value?.trim();
    if (!trimmed || seen.has(trimmed)) {
      continue;
    }
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

function getVisibleErrorCodes(errors: RunOutputError[]): string[] {
  return uniqueNonEmpty(errors.map(getErrorCode));
}

function getVisibleErrors(errors: RunOutputError[]): RunOutputError[] {
  return errors.filter((error) => getErrorMessage(error) !== null);
}

function hasRenderableErrors(errors: RunOutputError[]): boolean {
  return (
    getVisibleErrorCodes(errors).length > 0 ||
    getVisibleErrors(errors).length > 0
  );
}

function RunErrorsPanel({ errors }: { errors: RunOutputError[] }) {
  const visibleErrorCodes = getVisibleErrorCodes(errors);
  const visibleErrors = getVisibleErrors(errors);

  if (visibleErrorCodes.length === 0 && visibleErrors.length === 0) {
    return null;
  }

  return (
    <div className="rounded-md border border-destructive/35 bg-destructive/10 p-3">
      <div className="flex items-start gap-2">
        <ExclamationTriangleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <div className="text-xs font-semibold text-foreground">
              Run errors
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              Errors and failure codes from this run
            </div>
          </div>
          {visibleErrorCodes.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {visibleErrorCodes.map((code) => (
                <span
                  key={code}
                  className="rounded border border-destructive/30 bg-destructive/15 px-1.5 py-0.5 font-mono text-[11px] font-medium text-destructive"
                >
                  {code}
                </span>
              ))}
            </div>
          ) : null}
          {visibleErrors.length > 0 ? (
            <div className="space-y-2">
              {visibleErrors.map((error, index) => {
                const code = getErrorCode(error);
                const message = getErrorMessage(error);
                return (
                  <div
                    key={`${code ?? "error"}-${index}`}
                    className="rounded-md border border-destructive/20 bg-slate-elevation2 px-3 py-2 text-sm text-foreground"
                  >
                    <div className="flex min-w-0 items-start gap-2">
                      {code ? (
                        <span className="mt-0.5 shrink-0 rounded bg-destructive/15 px-1.5 py-0.5 font-mono text-[11px] text-destructive">
                          {code}
                        </span>
                      ) : null}
                      {message ? (
                        <span className="min-w-0 whitespace-pre-wrap break-words">
                          {message}
                        </span>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function RunOutputsSection({
  workflowRunId,
  workflowTitle,
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
  if (
    !hasExtracted &&
    files.length === 0 &&
    !hasErrors &&
    observerOutput == null &&
    !webhookFailureReason
  ) {
    return null;
  }

  const extractedJson = JSON.stringify(extractedInformation ?? {});

  return (
    <div className="flex flex-col gap-5">
      <RunErrorsPanel errors={errors} />
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
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              Extracted information
            </span>
            <SummarizeOutput
              contextKey={`run:${workflowRunId}`}
              outputJson={extractedJson}
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
          <OverviewCodeBlock
            value={JSON.stringify(extractedInformation, null, 2)}
            maxHeight="320px"
          />
        </div>
      ) : null}
      {files.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Downloaded files
          </span>
          <div className="flex flex-col gap-1">
            {files.map((file) => (
              <a
                key={file.url}
                href={file.url}
                title={file.url}
                aria-label={`Download ${file.filename}`}
                className="group flex items-center gap-2.5 rounded-md border border-border bg-slate-elevation2 px-3 py-2 text-sm text-foreground transition-colors hover:bg-slate-elevation3"
              >
                <FileIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{file.filename}</span>
                <DownloadIcon className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" />
              </a>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
