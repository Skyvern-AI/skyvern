import { ExclamationTriangleIcon } from "@radix-ui/react-icons";
import { CopyButton } from "@/components/CopyButton";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatSeconds } from "@/util/timeFormat";
import { workflowBlockTitle } from "@/routes/workflows/editor/nodes/types";
import { StatusDot } from "../WorkflowRunTimelineBlockItem";
import { WorkflowBlockIcon } from "@/routes/workflows/editor/nodes/WorkflowBlockIcon";
import { cn } from "@/util/utils";
import {
  type ObserverThought,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import type { WorkflowRunOverviewActiveElement } from "../WorkflowRunOverview";
import { ThoughtCard } from "../ThoughtCard";
import { CodeBlockFailureDetails } from "../CodeBlockFailureDetails";
import {
  describeCodeBlockFailure,
  failureSupportsScreenshot,
  lastBrowserStep,
} from "../codeBlockFailure";
import { useBlockScreenshot } from "../useBlockScreenshot";
import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";
import { FailureRecoveryActions } from "../../studio/runview/RunFailurePresentation";
import { formatFailureReason } from "../failureReasonFormat";
import { stringifyTimelineValue } from "./formatValue";

function TruncatedWithTooltip({
  full,
  className,
  side = "top",
}: {
  full: string;
  className?: string;
  side?: "top" | "bottom" | "left" | "right";
}) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn("min-w-0 truncate", className)}>{full}</span>
        </TooltipTrigger>
        <TooltipContent
          side={side}
          className="max-w-md break-all font-mono text-[11px]"
        >
          {full}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function isLoopBlock(block: WorkflowRunBlock): boolean {
  return block.block_type === "for_loop" || block.block_type === "while_loop";
}

function BlockDetailHeader({
  block,
  iterationOverride,
  runFinalized,
}: {
  block: WorkflowRunBlock;
  iterationOverride?: number | null;
  // A finalized run has no live block; without it a stale Running status would
  // spin here forever, the same way it would in the timeline row.
  runFinalized: boolean;
}) {
  const duration =
    block.duration !== null ? formatSeconds(block.duration) : null;
  const blockTypeTitle = workflowBlockTitle[block.block_type];
  // The backend mirrors current_index/current_value onto every block inside a
  // loop, but the timeline's "Iteration N · value" row one pane up already
  // says it for those — only the loop block itself states its own iteration.
  const isLoop = isLoopBlock(block);
  const hasIterationOverride =
    iterationOverride !== undefined && iterationOverride !== null;
  const hasResolvedIterationOverride =
    hasIterationOverride &&
    Array.isArray(block.loop_values) &&
    iterationOverride >= 0 &&
    iterationOverride < block.loop_values.length;
  const iterationIndex = hasResolvedIterationOverride
    ? iterationOverride
    : hasIterationOverride
      ? null
      : block.current_index;
  const iterationLabel =
    isLoop && iterationIndex !== null && iterationIndex !== undefined
      ? `Iteration ${iterationIndex + 1}`
      : null;
  // When a loop block has an explicit iteration selection, source the chip
  // value from loop_values[i] so it matches the iteration label. Without
  // this, the chip displays block.current_value (the latest iteration)
  // alongside a label for an older iteration — a contradictory pairing.
  let valueToShow: unknown = null;
  if (
    isLoop &&
    hasResolvedIterationOverride &&
    Array.isArray(block.loop_values)
  ) {
    const resolvedIterationIndex = iterationOverride as number;
    valueToShow = block.loop_values[resolvedIterationIndex];
  } else if (isLoop && !hasIterationOverride) {
    valueToShow = block.current_value;
  }
  const currentValueFull =
    valueToShow !== null && valueToShow !== undefined
      ? stringifyTimelineValue(valueToShow)
      : null;
  const currentValuePreview =
    currentValueFull !== null
      ? currentValueFull.replace(/\s+/g, " ").trim()
      : null;

  return (
    <div className="border-b border-border bg-slate-elevation1">
      <div
        data-slot="block-detail-header-primary"
        className="flex items-center gap-2 px-3 py-2"
      >
        <StatusDot status={block.status} isFinalized={runFinalized} />
        <WorkflowBlockIcon
          workflowBlockType={block.block_type}
          className="size-4 shrink-0 text-tertiary-foreground"
        />
        {/* The user's name for the block is its identity, the type its class —
            the same order the timeline row reads in (label first, type as the
            icon). Unlabeled blocks fall back to the type, as the row does. */}
        <TruncatedWithTooltip
          full={block.label ?? blockTypeTitle}
          className="text-sm font-semibold text-foreground"
        />
        {duration && (
          <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
            {duration}
          </span>
        )}
      </div>
      <div
        data-slot="block-detail-header-meta"
        className="flex min-w-0 items-center gap-1.5 px-3 pb-2 text-[11px] text-muted-foreground dark:text-slate-500"
      >
        {block.label && (
          <>
            <span className="shrink-0">{blockTypeTitle}</span>
            <span className="shrink-0 text-slate-600">·</span>
          </>
        )}
        <TruncatedWithTooltip
          full={block.workflow_run_block_id}
          className="max-w-[11rem] font-mono text-[10px] text-muted-foreground dark:text-slate-500"
        />
        {iterationLabel && (
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-tertiary-foreground">
            {iterationLabel}
          </span>
        )}
      </div>
      {currentValuePreview && (
        <div className="flex min-w-0 items-center gap-2 border-t border-border/50 px-3 py-1.5 text-[11px] duration-200 animate-in fade-in slide-in-from-top-1">
          <span className="shrink-0 text-muted-foreground dark:text-slate-500">
            Iterated value:
          </span>
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <code className="min-w-0 truncate rounded bg-slate-elevation1 px-1.5 py-0.5 font-mono text-tertiary-foreground">
                  {currentValuePreview}
                </code>
              </TooltipTrigger>
              <TooltipContent
                side="bottom"
                className="max-w-md break-all font-mono text-[11px]"
              >
                {currentValueFull}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      )}
      {block.description && (
        <div className="border-t border-border/50 px-3 py-2 duration-200 animate-in fade-in slide-in-from-top-1">
          <div className="text-xs text-muted-foreground">
            {block.description}
          </div>
        </div>
      )}
    </div>
  );
}

function BlockDetailHeaderSkeleton() {
  return (
    <div className="border-b border-border bg-slate-elevation1">
      <div className="flex items-center gap-2 px-3 py-2">
        <Skeleton className="size-3.5 shrink-0 rounded-full" />
        <Skeleton className="size-4 shrink-0 rounded" />
        <Skeleton className="h-4 w-24 rounded" />
        <Skeleton className="ml-auto h-3 w-10 shrink-0 rounded" />
      </div>
      <div className="flex items-center gap-1.5 px-3 pb-2">
        <Skeleton className="h-3 w-16 rounded" />
        <Skeleton className="h-3 w-28 rounded" />
      </div>
    </div>
  );
}

function Section({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "space-y-1.5 duration-200 animate-in fade-in slide-in-from-top-2",
        className,
      )}
    >
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground dark:text-slate-500">
        {title}
      </div>
      <div className="text-sm text-tertiary-foreground">{children}</div>
    </div>
  );
}

// Mounted only once the screenshot flow is in play: block detail renders in contexts that
// never set up a QueryClient, so the artifact query cannot live in the parent.
function BlockScreenshotThumbnail({
  workflowRunBlockId,
  blockType,
  onViewScreenshot,
}: {
  workflowRunBlockId: string;
  blockType?: string | null;
  onViewScreenshot: () => void;
}) {
  const screenshot = useBlockScreenshot(workflowRunBlockId, blockType, true);
  const { src, onImageError, imageFailed } = useArtifactImageSrc(screenshot);
  if (!screenshot || !src || imageFailed) return null;
  return (
    <figure className="mt-3 w-fit max-w-full">
      <button
        type="button"
        onClick={onViewScreenshot}
        aria-label="View block screenshot"
        className="block overflow-hidden rounded border border-border hover:border-foreground/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <img
          src={src}
          onError={onImageError}
          alt=""
          className="block max-h-40 max-w-full object-contain"
        />
      </button>
      <figcaption className="mt-1 text-[11px] text-muted-foreground">
        After the last browser step
      </figcaption>
    </figure>
  );
}

function BrowserContext({ block }: { block: WorkflowRunBlock }) {
  const step = lastBrowserStep(block.actions);
  const url = block.final_url?.trim() || null;
  if (!step && !url) return null;
  return (
    <div className="mt-2 space-y-0.5 text-muted-foreground">
      {step ? (
        <p>
          Last browser step: {step.label}
          {step.secondsBeforeError !== null
            ? `, ${step.secondsBeforeError.toFixed(1)} s before the error`
            : ""}
        </p>
      ) : null}
      {url ? (
        <p className="break-all">
          Page:{" "}
          {/^https?:\/\//i.test(url) ? (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-foreground underline underline-offset-2"
            >
              {url}
            </a>
          ) : (
            <span className="text-foreground">{url}</span>
          )}
        </p>
      ) : null}
    </div>
  );
}

function BlockDetailFailure({
  block,
  onViewScreenshot,
  onFix,
  statedFailureHeadline = null,
}: {
  block: WorkflowRunBlock;
  onViewScreenshot?: () => void;
  // Set only when the run is final and this card may offer Fix; the recovery gate is applied here.
  onFix?: () => void;
  // The exact headline text the Studio strip already states (only set for
  // the run's failing block). Compared by text, not just by block identity:
  // the strip's headline comes from the RUN's failure_reason, which can
  // differ from this block's own failure_reason even when this is the
  // block the run failed on — suppressing on identity alone would drop an
  // actionable reason the strip never actually showed.
  statedFailureHeadline?: string | null;
}) {
  const codeFailure = describeCodeBlockFailure(block);
  // Non-code failures have no curated title/guidance split like codeFailure
  // does, so borrow the strip's own headline/detail parser to find this
  // block's own headline and compare it against what the strip stated.
  const nonCodeReason =
    !codeFailure && block.failure_reason
      ? formatFailureReason(block.failure_reason)
      : null;
  const headlineElsewhere =
    codeFailure !== null
      ? statedFailureHeadline === codeFailure.title
      : statedFailureHeadline !== null &&
        nonCodeReason !== null &&
        statedFailureHeadline === nonCodeReason.headline;
  const nonCodeContent = headlineElsewhere
    ? nonCodeReason?.detail
    : block.failure_reason;
  if (!codeFailure && !nonCodeContent) return null;
  return (
    <div className="space-y-1.5 duration-200 animate-in fade-in slide-in-from-top-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-destructive">
        Failure
      </div>
      <div className="flex items-start gap-2.5 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-xs leading-relaxed text-foreground dark:bg-destructive/10">
        <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        {codeFailure ? (
          <div className="min-w-0 flex-1">
            {headlineElsewhere ? null : (
              <div className="line-clamp-3 break-words text-sm font-semibold leading-5">
                {codeFailure.title}
              </div>
            )}
            <p
              className={cn(
                "break-words text-muted-foreground",
                !headlineElsewhere && "mt-1",
              )}
            >
              {codeFailure.guidance}
            </p>
            <BrowserContext block={block} />
            {onFix && codeFailure.recovery !== "retry" ? (
              <div className="mt-3 flex flex-wrap gap-2">
                <FailureRecoveryActions onFix={onFix} />
              </div>
            ) : null}
            {onViewScreenshot && failureSupportsScreenshot(codeFailure) ? (
              <BlockScreenshotThumbnail
                workflowRunBlockId={block.workflow_run_block_id}
                blockType={block.block_type}
                onViewScreenshot={onViewScreenshot}
              />
            ) : null}
            <CodeBlockFailureDetails
              failure={codeFailure}
              reason={block.failure_reason}
            />
          </div>
        ) : (
          <span className="min-w-0 flex-1 break-words">{nonCodeContent}</span>
        )}
      </div>
    </div>
  );
}

function CodeBlock({
  children,
  className,
  copyValue,
}: {
  children: React.ReactNode;
  className?: string;
  copyValue?: string;
}) {
  // When copyValue is provided, render the block with a hover-revealed copy
  // affordance in the top-right corner. The wrapper has `group` so the
  // CopyButton uses opacity transitions to fade in.
  if (copyValue !== undefined) {
    return (
      <div className={cn("group relative", className)}>
        <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-slate-elevation1 p-2.5 pr-10 font-mono text-xs text-foreground dark:text-slate-200">
          {children}
        </pre>
        <div className="absolute right-1 top-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
          <CopyButton value={copyValue} />
        </div>
      </div>
    );
  }
  return (
    <pre
      className={cn(
        "overflow-x-auto whitespace-pre-wrap break-all rounded bg-slate-elevation1 p-2.5 font-mono text-xs text-foreground dark:text-slate-200",
        className,
      )}
    >
      {children}
    </pre>
  );
}

function JsonView({ value }: { value: unknown }) {
  const text = stringifyTimelineValue(value);
  return <CodeBlock copyValue={text}>{text}</CodeBlock>;
}

function GoalText({ text }: { text: string | null | undefined }) {
  if (!text) return null;
  return (
    <div className="whitespace-pre-wrap text-sm leading-relaxed text-tertiary-foreground">
      {text}
    </div>
  );
}

function BlockThoughtList({
  thoughts,
  activeItem,
  onSelect,
}: {
  thoughts: Array<ObserverThought>;
  activeItem: WorkflowRunOverviewActiveElement;
  onSelect?: (thought: ObserverThought) => void;
}) {
  if (thoughts.length === 0) return null;
  return (
    <Section title={`Thoughts (${thoughts.length})`}>
      <div className="space-y-2">
        {thoughts.map((thought) => {
          const isActive =
            activeItem !== null &&
            activeItem !== "stream" &&
            typeof activeItem === "object" &&
            "thought_id" in activeItem &&
            activeItem.thought_id === thought.thought_id;
          return (
            <ThoughtCard
              key={thought.thought_id}
              active={isActive}
              thought={thought}
              onClick={() => onSelect?.(thought)}
            />
          );
        })}
      </div>
    </Section>
  );
}

export {
  BlockDetailFailure,
  BlockDetailHeader,
  BlockDetailHeaderSkeleton,
  BlockThoughtList,
  CodeBlock,
  GoalText,
  JsonView,
  Section,
};
