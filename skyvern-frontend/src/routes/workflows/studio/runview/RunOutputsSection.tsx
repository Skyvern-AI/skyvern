import { DownloadIcon, FileIcon } from "@radix-ui/react-icons";

import { SummarizeOutput } from "@/components/SummarizeOutput";

import { OverviewCodeBlock } from "./OverviewCodeBlock";

export type RunOutputFile = { url: string; filename: string };

type RunOutputsSectionProps = {
  workflowRunId: string;
  workflowTitle?: string | null;
  extractedInformation: Record<string, unknown> | null;
  files: RunOutputFile[];
  // Owned by RunView so the generated summary survives center-tab switches
  // (this section unmounts when another tab takes the center).
  summary: string | null;
  onSummary: (summary: string | null) => void;
};

export function RunOutputsSection({
  workflowRunId,
  workflowTitle,
  extractedInformation,
  files,
  summary,
  onSummary,
}: RunOutputsSectionProps) {
  const hasExtracted =
    extractedInformation != null &&
    Object.values(extractedInformation).some((value) => value !== null);
  if (!hasExtracted && files.length === 0) {
    return null;
  }

  const extractedJson = JSON.stringify(extractedInformation ?? {});

  return (
    <div className="flex flex-col gap-5">
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
                className="group flex items-center gap-2.5 rounded-md border border-border bg-slate-elevation2 px-3 py-2 text-sm text-foreground transition-colors hover:border-studio-accent/40 hover:bg-slate-elevation3"
              >
                <FileIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{file.filename}</span>
                <DownloadIcon className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-studio-accent-2" />
              </a>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
