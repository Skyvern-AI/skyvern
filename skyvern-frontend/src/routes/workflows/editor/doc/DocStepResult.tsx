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
import { StatusDot } from "../../workflowRun/StatusDot";
import {
  HeroScreenshot,
  type HeroSelection,
} from "../../studio/runview/HeroScreenshot";
import { useHeroScreenshot } from "../../studio/runview/useHeroScreenshot";
import { useBlockScreenshot } from "../../workflowRun/useBlockScreenshot";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { useBlockRunTarget } from "../hooks/useBlockRunTarget";
import { useDocRun } from "./useDocRun";
import {
  blockFailureHeadline,
  docLoopProgressText,
  docRunStoppedByStep,
  runFailureHeadline,
  stoppedOnPurpose,
  type DocLoopItem,
  type DocLoopProgress,
} from "./docRunState";

function DocLoopItemLine({ item }: { item: DocLoopItem }) {
  const [open, setOpen] = useState(false);
  const words = (
    <span className="min-w-0 [overflow-wrap:anywhere]">
      Item {item.index + 1}
      {item.title ? ` · ${item.title}` : ""}
      {item.state === "failed" && item.failureHeadline
        ? ` · ${item.failureHeadline}`
        : ""}
    </span>
  );
  const dot = (
    <span className="mt-[3px] flex">
      <StatusDot
        status={
          item.state === "failed"
            ? Status.Failed
            : item.state === "running"
              ? Status.Running
              : item.state === "waiting"
                ? Status.Paused
                : Status.Completed
        }
        isFinalized={item.state !== "running"}
      />
    </span>
  );
  return (
    <li
      className={cn(
        "min-w-0",
        item.state === "failed" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      {item.detail ? (
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? "Hide" : "Show"} item ${item.index + 1}`}
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 items-start gap-1.5 rounded text-left outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronRightIcon
            className={cn(
              "mt-[3px] size-3 shrink-0 transition-transform",
              open && "rotate-90",
            )}
          />
          {dot}
          {words}
        </button>
      ) : (
        <div className="flex min-w-0 items-start gap-1.5 pl-[18px]">
          {dot}
          {words}
        </div>
      )}
      {open && item.detail && (
        <pre
          className="ml-[18px] mt-1 max-h-[240px] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted p-2 font-mono text-xs text-foreground [overflow-wrap:anywhere]"
          data-testid="doc-loop-item-value"
        >
          {item.detail}
        </pre>
      )}
    </li>
  );
}

function DocLoopItems({ progress }: { progress: DocLoopProgress }) {
  return (
    <ul
      className="mt-1 space-y-0.5 pl-5 text-[13px]"
      data-testid="doc-loop-items"
    >
      {progress.items.map((item) => (
        <DocLoopItemLine key={item.index} item={item} />
      ))}
    </ul>
  );
}

// Same short headline the Run panel states; the full text is in the expanded inspector.
function failureLine(block: WorkflowRunBlock): string {
  const headline = blockFailureHeadline(block);
  return headline ? `Failed: ${headline}` : "Failed";
}

function resultText(
  block: WorkflowRunBlock,
  runLive: boolean,
  completedText?: string,
): string {
  switch (block.status) {
    case Status.Completed:
      return getBlockResultSummary(block) ?? completedText ?? "Completed";
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

function DocLiveThumbnail({ block }: { block: WorkflowRunBlock }) {
  const selection: HeroSelection = {
    kind: "block",
    workflowRunBlockId: block.workflow_run_block_id,
    blockType: block.block_type,
  };
  const { screenshot } = useHeroScreenshot(selection, true);
  const [unavailable, setUnavailable] = useState(false);
  // No box until a capture that can be shown exists: a placeholder says nothing.
  const hidden = !screenshot || unavailable;
  return (
    <div
      hidden={hidden}
      className={cn(
        "mt-1.5 aspect-video w-full max-w-[280px] overflow-hidden rounded-md border border-border bg-muted",
        hidden && "hidden",
      )}
      data-testid="doc-step-live-thumbnail"
    >
      <div className="relative h-full w-full">
        <HeroScreenshot
          selection={selection}
          running
          onUnavailable={setUnavailable}
        />
      </div>
    </div>
  );
}

export function DocStepResult({
  label,
  completedText,
}: {
  label: string;
  completedText?: string;
}) {
  const {
    runId,
    run,
    blocksByLabel,
    loopsByLabel,
    live,
    blockRun,
    unknownLabels,
    ranLabels,
    rowsLoaded,
  } = useDocRun();
  const [open, setOpen] = useState(false);
  const { blockLabel: targetLabel } = useBlockRunTarget();
  const block = blocksByLabel.get(label);
  const screenshot = useBlockScreenshot(
    block?.workflow_run_block_id,
    block?.block_type,
    open,
  );
  if (!runId || unknownLabels.has(label)) return null;
  if (!block) {
    // A one-step run can fail before its step has a row (a missing input or credential); the line says so.
    const runFailure = run && !live ? runFailureHeadline(run) : null;
    if (blockRun && rowsLoaded && label === targetLabel && runFailure)
      return (
        <div
          className="mb-1 mt-1 text-[13px] text-destructive"
          data-testid="doc-step-result"
          data-doc-run-failed
        >
          Failed: {runFailure}
        </div>
      );
    if (!run || blockRun || !rowsLoaded) return null;
    if (ranLabels && !ranLabels.has(label)) return null;
    // A live run has not got here yet: say so, so every step in the run shows one state.
    if (live) {
      return (
        <div
          className="mb-1 mt-1 flex items-center gap-1.5 text-[13px] text-muted-foreground"
          data-testid="doc-step-result"
          data-doc-waiting
        >
          <StatusDot status={Status.Queued} isFinalized={false} />
          Waiting to run
        </div>
      );
    }
    return (
      <div
        className="mb-1 mt-1 text-[13px] text-muted-foreground"
        data-testid="doc-step-result"
        data-doc-not-reached
      >
        {docRunStoppedByStep(blocksByLabel)
          ? "Not reached, the run stopped above"
          : "Not reached"}
      </div>
    );
  }
  const loop = loopsByLabel.get(label);
  const loopText = loop ? docLoopProgressText(loop, live) : null;

  const running = block.status === Status.Running && live;
  const duration = running ? null : formatBlockDuration(block);
  const text = resultText(block, live, completedText);
  const files = getBlockDownloadedFileUrls(
    block.output,
    run?.downloaded_file_urls ?? [],
  );
  // A Stop step that stopped the run did what the doc says: not a failure (its tag is green too).
  const intentionalStop = stoppedOnPurpose(block);
  const failed =
    !intentionalStop &&
    (block.status === Status.Failed ||
      block.status === Status.TimedOut ||
      block.status === Status.Terminated);

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
            <StatusDot
              status={intentionalStop ? Status.Completed : block.status}
              isFinalized={!live}
            />
          </span>
          <span
            className={cn(
              "min-w-0",
              failed
                ? "truncate text-destructive"
                : "text-muted-foreground [overflow-wrap:anywhere]",
            )}
          >
            {loopText && block.status !== Status.Failed ? loopText : text}
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
      {loop && loop.items.length > 0 && (live || open) && (
        <DocLoopItems progress={loop} />
      )}
      {running && !loop && <DocLiveThumbnail block={block} />}
      {open && (
        <div className="mt-1.5 min-w-0 space-y-3 overflow-x-auto rounded-md border border-border">
          <BlockInspector block={block} wrapTabs />
          {!running && screenshot && (
            <div className="relative mx-3 mb-3 aspect-video max-w-[480px] overflow-hidden rounded-md border border-border bg-muted">
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
