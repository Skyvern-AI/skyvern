import { useState } from "react";
import { ChevronRightIcon, ExternalLinkIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { Status } from "@/api/types";
import { cn } from "@/util/utils";
import { workflowRunDetailPath } from "../../studioNavigation";
import { BlockInspector } from "../../workflowRun/blockDetail/BlockInspector";
import {
  formatBlockDuration,
  getBlockResultSummary,
} from "../../workflowRun/blockResult";
import {
  filenameForDownloadedFileUrl,
  getBlockDownloadedFileUrls,
} from "../../workflowRun/blockDownloadedFiles";
import { describeCodeBlockFailure } from "../../workflowRun/codeBlockFailure";
import { formatFailureReason } from "../../workflowRun/failureReasonFormat";
import { StatusDot } from "../../workflowRun/StatusDot";
import { HeroScreenshot } from "../../studio/runview/HeroScreenshot";
import { useBlockScreenshot } from "../../workflowRun/useBlockScreenshot";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { useDocRun } from "./useDocRun";

// Same short headline the Run panel states; the full text is in the expanded inspector.
function failureLine(block: WorkflowRunBlock): string {
  if (!block.failure_reason?.trim()) return "Failed";
  const headline =
    describeCodeBlockFailure(block)?.title ??
    formatFailureReason(block.failure_reason).headline;
  return headline ? `Failed: ${headline}` : "Failed";
}

function resultText(block: WorkflowRunBlock, runLive: boolean): string {
  switch (block.status) {
    case Status.Completed:
      return getBlockResultSummary(block) ?? "Completed";
    case Status.Running:
      return runLive ? "Running…" : "Stopped";
    case Status.Queued:
    case Status.Created:
      return "Waiting to run";
    case Status.Paused:
      return "Paused, waiting for you";
    case Status.Skipped:
      return "Skipped";
    case Status.Canceled:
      return "Canceled";
    case Status.Terminated:
      return failureLine(block).replace(/^Failed/, "Stopped");
    case Status.Failed:
    case Status.TimedOut:
      return failureLine(block);
    default:
      return "Not run";
  }
}

export function DocStepResult({ label }: { label: string }) {
  const { runId, run, blocksByLabel, live } = useDocRun();
  const [open, setOpen] = useState(false);
  const block = blocksByLabel.get(label);
  const screenshot = useBlockScreenshot(
    block?.workflow_run_block_id,
    block?.block_type,
    open,
  );
  if (!runId || !block) return null;

  const running = block.status === Status.Running && live;
  const duration = running ? null : formatBlockDuration(block);
  const text = resultText(block, live);
  const files = getBlockDownloadedFileUrls(
    block.output,
    run?.downloaded_file_urls ?? [],
  );
  const failed =
    block.status === Status.Failed ||
    block.status === Status.TimedOut ||
    block.status === Status.Terminated;

  return (
    <div className="mb-1 mt-1 min-w-0" data-testid="doc-step-result">
      <div className="flex min-h-[24px] flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? "Hide" : "Show"} result of ${label}`}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex min-w-0 items-start gap-1.5 rounded text-left outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            className={cn(
              "mt-[3px] size-3.5 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
          <span className="mt-[3px] flex">
            <StatusDot status={block.status} isFinalized={!live} />
          </span>
          <span
            className={cn(
              "min-w-0 [overflow-wrap:anywhere]",
              failed ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {text}
            {duration && ` · ${duration}`}
          </span>
        </button>
        {files.length > 0 && !open && (
          <span className="text-xs text-muted-foreground">
            {files.length} file{files.length === 1 ? "" : "s"}
          </span>
        )}
        <Link
          to={workflowRunDetailPath(runId)}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 outline-none hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring"
        >
          Open run
          <ExternalLinkIcon className="size-3" />
        </Link>
      </div>
      {running && (
        <div
          className="mt-1.5 aspect-video w-full max-w-[280px] overflow-hidden rounded-md border border-border bg-muted"
          data-testid="doc-step-live-thumbnail"
        >
          <div className="relative h-full w-full">
            <HeroScreenshot
              selection={{
                kind: "block",
                workflowRunBlockId: block.workflow_run_block_id,
                blockType: block.block_type,
              }}
              running
              unavailableText="Waiting for the first capture…"
            />
          </div>
        </div>
      )}
      {open && (
        <div className="mt-1.5 min-w-0 space-y-3 overflow-x-auto rounded-md border border-border">
          <BlockInspector block={block} wrapTabs />
          {!running && screenshot && (
            <div className="relative mx-3 aspect-video max-w-[480px] overflow-hidden rounded-md border border-border bg-muted">
              <HeroScreenshot
                selection={{
                  kind: "block",
                  workflowRunBlockId: block.workflow_run_block_id,
                  blockType: block.block_type,
                }}
                running={false}
              />
            </div>
          )}
          {files.length > 0 && (
            <ul className="mx-3 mb-3 space-y-1 text-[13px]">
              {files.map((url) => (
                <li key={url}>
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary underline-offset-2 hover:underline"
                  >
                    {filenameForDownloadedFileUrl(url)}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
