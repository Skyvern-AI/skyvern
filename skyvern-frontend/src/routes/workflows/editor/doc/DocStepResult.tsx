import { useEffect, useState } from "react";
import {
  ChevronRightIcon,
  ExternalLinkIcon,
  MinusCircledIcon,
} from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { ArtifactType, Status, type ArtifactApiResponse } from "@/api/types";
import { cn } from "@/util/utils";
import { workflowRunDetailPath } from "../../studioNavigation";
import { JsonExplorer } from "../../workflowRun/blockDetail/BlockInspector";
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
import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";
import { useHeroScreenshot } from "../../studio/runview/useHeroScreenshot";
import { useBlockScreenshot } from "../../workflowRun/useBlockScreenshot";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { useBlockRunTarget } from "../hooks/useBlockRunTarget";
import { docCellPreview } from "./docValuePreview";
import { useDocRunFor } from "./useDocRunFor";
import {
  docFailureDetail,
  docFailureGuidance,
  docProduced,
  returnsAValue,
  type DocProduced,
} from "./docStepProduced";
import {
  blockFailureHeadline,
  docLoopProgressText,
  docResultAge,
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
        ? ` · Failed: ${item.failureHeadline}`
        : ""}
    </span>
  );
  const dot = (
    <span className="mt-[3px] flex">
      <ResultDot
        status={
          item.state === "failed"
            ? Status.Failed
            : item.state === "canceled"
              ? Status.Canceled
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
        item.state === "failed"
          ? "text-red-700 dark:text-red-400"
          : "text-muted-foreground",
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

const LIST_CAP = 5;

function DocLoopItems({ progress }: { progress: DocLoopProgress }) {
  // Failed items first, so a long loop never hides the one that went wrong.
  const ordered = [
    ...progress.items.filter((item) => item.state === "failed"),
    ...progress.items.filter((item) => item.state !== "failed"),
  ];
  const more = ordered.length - LIST_CAP;
  return (
    <ul
      className="space-y-0.5 border-l border-border pl-3 text-[13px]"
      data-testid="doc-loop-items"
    >
      {ordered.slice(0, LIST_CAP).map((item) => (
        <DocLoopItemLine key={item.index} item={item} />
      ))}
      {more > 0 && (
        <li className="pl-[18px] text-muted-foreground">
          {more} more item{more === 1 ? "" : "s"}
        </li>
      )}
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
  runCanceled: boolean,
  completedText?: string,
): string {
  switch (block.status) {
    case Status.Completed:
      return getBlockResultSummary(block) ?? completedText ?? "Completed";
    case Status.Running:
      return runLive ? "Running…" : runCanceled ? "Canceled" : "Stopped";
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

function CellValue({ value }: { value: unknown }) {
  const { text, title, href } = docCellPreview(value);
  if (!href) return <span title={title}>{text}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={title}
      className="underline decoration-muted-foreground underline-offset-2 hover:decoration-foreground"
    >
      {text}
    </a>
  );
}

const CELL = "px-2.5 py-1 align-top [overflow-wrap:anywhere]";

function ProducedValues({ produced }: { produced: DocProduced }) {
  if (produced.kind === "json") {
    return (
      <div className="max-h-[240px] min-w-0 overflow-auto text-xs">
        <JsonExplorer value={produced.value} rootLabel="output" />
      </div>
    );
  }
  return (
    <div
      className="overflow-x-auto rounded-md border border-border text-[13px]"
      style={{ width: "max-content", maxWidth: "100%" }}
      data-testid="doc-step-values"
    >
      <table className="border-collapse text-left">
        {produced.kind === "table" ? (
          <>
            <thead>
              <tr className="bg-muted font-medium text-muted-foreground">
                {produced.columns.map((column) => (
                  <th
                    scope="col"
                    key={column}
                    className={cn(CELL, "whitespace-nowrap font-medium")}
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {produced.rows.map((row, index) => (
                <tr key={index} className="border-t border-border">
                  {produced.columns.map((column) => (
                    <td
                      key={column}
                      className={cn(
                        CELL,
                        typeof row[column] === "number" &&
                          "whitespace-nowrap text-right tabular-nums",
                      )}
                    >
                      <CellValue value={row[column]} />
                    </td>
                  ))}
                </tr>
              ))}
              {produced.moreRows > 0 && (
                <tr className="border-t border-border text-muted-foreground">
                  <td className={CELL} colSpan={produced.columns.length}>
                    {produced.moreRows} more row
                    {produced.moreRows === 1 ? "" : "s"}
                  </td>
                </tr>
              )}
            </tbody>
          </>
        ) : (
          <tbody>
            {produced.pairs.map(([field, value]) => (
              <tr
                key={field}
                className="border-t border-border first:border-t-0"
              >
                <th
                  scope="row"
                  className={cn(
                    CELL,
                    "bg-muted font-medium text-muted-foreground",
                  )}
                >
                  {field}
                </th>
                <td
                  className={cn(
                    CELL,
                    typeof value === "number" &&
                      "whitespace-nowrap text-right tabular-nums",
                  )}
                >
                  <CellValue value={value} />
                </td>
              </tr>
            ))}
          </tbody>
        )}
      </table>
    </div>
  );
}

// Mounted before the step is opened, hidden, so the chevron only appears for a screen that really loads.
function DocLastScreen({
  artifact,
  label,
  visible,
  onLoadedChange,
}: {
  artifact: ArtifactApiResponse;
  label: string;
  visible: boolean;
  onLoadedChange: (loadedArtifactId: string | null) => void;
}) {
  const { src, onImageError, imageFailed } = useArtifactImageSrc(artifact);
  const usable = !artifact.archived && !imageFailed && Boolean(src);
  useEffect(() => {
    if (!usable) onLoadedChange(null);
  }, [usable, onLoadedChange]);
  if (!usable) return null;
  return (
    <img
      src={src}
      alt={`Last screen of ${label}`}
      onLoad={() => onLoadedChange(artifact.artifact_id)}
      onError={onImageError}
      hidden={!visible}
      className="h-auto w-full max-w-[280px] rounded-md border border-border"
      data-testid="doc-step-last-screen"
    />
  );
}

function ResultDot({
  status,
  isFinalized,
}: {
  status: Status | null;
  isFinalized: boolean;
}) {
  // Canceled is a choice, not an error: the shared dot draws it red, the Doc draws it as grey as Skipped.
  if (status === Status.Canceled) {
    return (
      <span
        role="img"
        aria-label="canceled"
        title="canceled"
        className="flex shrink-0 items-center"
      >
        <MinusCircledIcon className="size-3.5 shrink-0 text-muted-foreground" />
      </span>
    );
  }
  return <StatusDot status={status} isFinalized={isFinalized} />;
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
    runsByLabel,
    live,
    blockRun,
    unknownLabels,
    ranLabels,
    rowsLoaded,
    older,
  } = useDocRunFor(label);
  const [openBlockId, setOpenBlockId] = useState<string | null>(null);
  const [loadedScreenId, setLoadedScreenId] = useState<string | null>(null);
  const { blockLabel: targetLabel } = useBlockRunTarget();
  const block = blocksByLabel.get(label);
  // Tied to the block row: the next run of this step starts closed.
  const open = openBlockId === (block?.workflow_run_block_id ?? null);
  // Fetched before the step is opened: whether it has a screen decides whether there is anything to expand.
  const settled =
    block !== undefined &&
    block.status !== Status.Running &&
    block.status !== Status.Queued &&
    block.status !== Status.Created &&
    block.status !== Status.Skipped;
  // A loop or If step only groups other steps: its own last screen says nothing about the work.
  const groupsSteps =
    block?.block_type === "for_loop" ||
    block?.block_type === "while_loop" ||
    block?.block_type === "conditional";
  const fetchedScreen = useBlockScreenshot(
    block?.workflow_run_block_id,
    block?.block_type,
    settled && !groupsSteps,
  );
  // A Code step's only LLM screen is the one taken before it ran; only a screen from its own actions counts.
  const screenshot =
    block?.block_type === "code" &&
    fetchedScreen?.artifact_type !== ArtifactType.ActionScreenshot
      ? undefined
      : fetchedScreen;
  if (!runId || unknownLabels.has(label)) return null;
  if (!block) {
    // A one-step run can fail before its step has a row (a missing input or credential); the line says so.
    const runFailure = run && !live ? runFailureHeadline(run) : null;
    if (blockRun && rowsLoaded && label === targetLabel && runFailure)
      return (
        <div
          className="mb-1 mt-1 text-[13px] text-red-700 [overflow-wrap:anywhere] dark:text-red-400"
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
  const runCanceled = run?.status === Status.Canceled;
  // A looped step ran once per item; one run's result is never the whole step's.
  const runs = runsByLabel?.get(label);
  const ranManyTimes =
    !loop &&
    runs !== undefined &&
    runs.count > 1 &&
    (block.status === Status.Completed ||
      block.status === Status.Failed ||
      block.status === Status.TimedOut ||
      block.status === Status.Terminated ||
      block.status === Status.Canceled);
  const duration = running || ranManyTimes ? null : formatBlockDuration(block);
  const age = older ? docResultAge(block.modified_at, Date.now()) : null;
  const baseText = resultText(block, live, runCanceled, completedText);
  const allFiles = getBlockDownloadedFileUrls(
    block.output,
    run?.downloaded_file_urls ?? [],
  );
  const files = ranManyTimes ? [] : allFiles;
  // A Stop step that stopped the run did what the doc says: not a failure (its tag is green too).
  const intentionalStop = stoppedOnPurpose(block);
  const failed =
    !intentionalStop &&
    (block.status === Status.Failed ||
      block.status === Status.TimedOut ||
      block.status === Status.Terminated);
  const produced = running || ranManyTimes ? null : docProduced(block);
  // The shared summary counts the wrapper key ("Found 1 field"); the line must match the table under it.
  const text =
    produced?.kind === "table" && /^Found \d+ fields?$/.test(baseText)
      ? `Found ${produced.rows.length + produced.moreRows} item${produced.rows.length + produced.moreRows === 1 ? "" : "s"}`
      : baseText;
  const failureDetail = failed ? docFailureDetail(block) : null;
  const failureGuidance = failed ? docFailureGuidance(block) : null;
  // The query shares its cache with the Run pane, so a disabled fetch can still return a screen.
  const lastScreen =
    running || ranManyTimes || groupsSteps ? undefined : screenshot;
  const hasScreen =
    lastScreen !== undefined && loadedScreenId === lastScreen.artifact_id;
  const loopItems = loop && loop.items.length > 0 ? loop : null;
  const expandable =
    !running &&
    Boolean(
      produced ||
      files.length ||
      hasScreen ||
      loopItems ||
      failureDetail ||
      failureGuidance,
    );
  const returnedNothing =
    block.status === Status.Completed &&
    !loop &&
    returnsAValue(block) &&
    (ranManyTimes ? runs.withOutput === 0 : !expandable && !lastScreen);
  const ranText = runs
    ? `Ran ${runs.count} times${runs.failed ? ` · ${runs.failed} failed` : ""}${runs.canceled ? ` · ${runs.canceled} canceled` : ""}`
    : text;
  const canceled =
    block.status === Status.Canceled ||
    (block.status === Status.Running && !live && runCanceled);
  const shownText = ranManyTimes
    ? ranText
    : loopText && block.status !== Status.Failed && !canceled
      ? loopText
      : text;
  const anyFailed =
    (loop?.items.some((item) => item.state === "failed") ?? false) ||
    (ranManyTimes && (runs?.failed ?? 0) > 0);
  const dotStatus: Status | null = intentionalStop
    ? Status.Completed
    : anyFailed &&
        (block.status === Status.Completed ||
          (block.status === Status.Canceled && ranManyTimes))
      ? Status.Failed
      : canceled
        ? Status.Canceled
        : block.status;
  const failedCount =
    loopText || ranManyTimes
      ? shownText.match(/^(.*?)( · \d+ failed)(.*)$/)
      : null;
  const expanded = expandable && open;

  const lineContent = (
    <>
      {expandable ? (
        <ChevronRightIcon
          className={cn(
            "mt-[3px] size-3.5 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-90",
          )}
        />
      ) : (
        <span className="size-3.5 shrink-0" aria-hidden />
      )}
      <span className="mt-[3px] flex">
        <ResultDot status={dotStatus} isFinalized={!live} />
      </span>
      <span
        className={cn(
          "min-w-0",
          failed
            ? "text-red-700 [overflow-wrap:anywhere] dark:text-red-400"
            : "text-muted-foreground [overflow-wrap:anywhere]",
        )}
      >
        {failedCount ? (
          <>
            {failedCount[1]}
            <span className="text-red-700 dark:text-red-400">
              {failedCount[2]}
            </span>
            {failedCount[3]}
          </>
        ) : (
          shownText
        )}
        {duration && ` · ${duration}`}
        {returnedNothing && " · returned nothing"}
        {age && (
          <span className="text-muted-foreground" data-doc-result-age>
            {` · ${age}`}
          </span>
        )}
      </span>
    </>
  );

  return (
    <div className="mb-1 mt-1 min-w-0" data-testid="doc-step-result">
      <div className="flex min-h-[24px] flex-wrap items-center gap-x-2 gap-y-1 pl-[22px] text-[13px]">
        {expandable ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={`${expanded ? "Hide" : "Show"} result of ${label}`}
            onClick={() =>
              setOpenBlockId(open ? null : block.workflow_run_block_id)
            }
            className="-ml-[22px] inline-flex min-w-0 items-start gap-1.5 rounded text-left outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            {lineContent}
          </button>
        ) : (
          <div className="-ml-[22px] inline-flex min-w-0 items-start gap-1.5">
            {lineContent}
          </div>
        )}
        {files.length > 0 && !expanded && (
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
      {lastScreen && !expanded && (
        <div hidden>
          <DocLastScreen
            artifact={lastScreen}
            label={label}
            visible={false}
            onLoadedChange={setLoadedScreenId}
          />
        </div>
      )}
      {(running && !loop) || (loopItems && (live || expanded)) || expanded ? (
        <div className="ml-[22px] mt-1.5 min-w-0 space-y-2 pb-1">
          {running && !loop && <DocLiveThumbnail block={block} />}
          {loopItems && (live || expanded) && (
            <DocLoopItems progress={loopItems} />
          )}
          {expanded && (
            <>
              {failureDetail && (
                <p className="text-[13px] text-foreground [overflow-wrap:anywhere]">
                  {failureDetail}
                </p>
              )}
              {failureGuidance && (
                <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
                  {failureGuidance}
                </p>
              )}
              {produced && <ProducedValues produced={produced} />}
              {lastScreen && (
                <DocLastScreen
                  artifact={lastScreen}
                  label={label}
                  visible
                  onLoadedChange={setLoadedScreenId}
                />
              )}
              {files.length > 0 && (
                <ul className="space-y-1 text-[13px]">
                  {files.slice(0, LIST_CAP).map((url) => (
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
                  {files.length > LIST_CAP && (
                    <li className="text-muted-foreground">
                      {files.length - LIST_CAP} more file
                      {files.length - LIST_CAP === 1 ? "" : "s"}
                    </li>
                  )}
                </ul>
              )}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
