import { useEffect, useState } from "react";
import { FileIcon, FileTextIcon } from "@radix-ui/react-icons";

import { SummarizeOutput } from "@/components/SummarizeOutput";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { cn } from "@/util/utils";

export type RunOutputFile = { url: string; filename: string };

type RunOutputsButtonProps = {
  workflowRunId: string;
  workflowTitle?: string | null;
  extractedInformation: Record<string, unknown> | null;
  files: RunOutputFile[];
};

export function RunOutputsButton({
  workflowRunId,
  workflowTitle,
  extractedInformation,
  files,
}: RunOutputsButtonProps) {
  const [summary, setSummary] = useState<string | null>(null);
  useEffect(() => {
    setSummary(null);
  }, [workflowRunId]);

  const hasExtracted =
    extractedInformation != null &&
    Object.values(extractedInformation).some((value) => value !== null);
  if (!hasExtracted && files.length === 0) {
    return null;
  }

  const extractedJson = JSON.stringify(extractedInformation ?? {});

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
            "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          <FileTextIcon className="h-4 w-4" />
          Outputs
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="flex w-[30rem] max-w-[90vw] flex-col gap-4"
      >
        {hasExtracted ? (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-foreground">
                Extracted information
              </span>
              <SummarizeOutput
                contextKey={`run:${workflowRunId}`}
                outputJson={extractedJson}
                workflowTitle={workflowTitle}
                hasSummary={summary !== null}
                onSummary={setSummary}
              />
            </div>
            {summary !== null ? (
              <div
                role="status"
                aria-live="polite"
                className="whitespace-pre-wrap rounded bg-slate-elevation3 p-3 text-sm"
              >
                {summary}
              </div>
            ) : null}
            <CodeEditor
              language="json"
              value={JSON.stringify(extractedInformation, null, 2)}
              readOnly
              maxHeight="220px"
            />
          </div>
        ) : null}
        {files.length > 0 ? (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-foreground">
              Downloaded files
            </span>
            <ScrollArea>
              <ScrollAreaViewport className="max-h-[180px]">
                <div className="flex flex-col gap-2">
                  {files.map((file) => (
                    <div
                      key={file.url}
                      title={file.url}
                      className="flex items-center gap-2 text-sm"
                    >
                      <FileIcon className="size-4 shrink-0" />
                      <a
                        href={file.url}
                        className="truncate underline underline-offset-4"
                      >
                        {file.filename}
                      </a>
                    </div>
                  ))}
                </div>
              </ScrollAreaViewport>
            </ScrollArea>
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
