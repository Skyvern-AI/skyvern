import { useState } from "react";
import { ArrowRightIcon } from "@radix-ui/react-icons";
import { Handle, Position } from "@xyflow/react";

import { cn } from "@/util/utils";

import {
  diffWords,
  type ReviewFieldChange,
  type ReviewStatus,
  type TextSegment,
} from "../panels/workflowReviewDiff";
import {
  REVIEW_STATUS_META,
  type BlockReviewAnnotation,
  type StartReviewAnnotation,
} from "./reviewAnnotation";

function ReviewStatusChip({
  status,
  count,
  className,
}: {
  status: ReviewStatus;
  count?: number;
  className?: string;
}) {
  const meta = REVIEW_STATUS_META[status];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium",
        meta.chip,
        className,
      )}
    >
      <span aria-hidden className={cn("font-semibold", meta.glyphClass)}>
        {meta.glyph}
      </span>
      {count === undefined ? meta.word : `${count} ${meta.word.toLowerCase()}`}
    </span>
  );
}

const SHORT_VALUE_LENGTH = 48;

function isShortValue(value: string | null): boolean {
  return (
    value === null ||
    (value.length <= SHORT_VALUE_LENGTH && !value.includes("\n"))
  );
}

function NotSet() {
  return <span className="italic text-muted-foreground">Not set</span>;
}

function DiffLine({
  tone,
  segments,
}: {
  tone: "before" | "after";
  segments: Array<TextSegment>;
}) {
  const before = tone === "before";
  return (
    <div
      className={cn(
        "nowheel flex max-h-40 gap-2 overflow-auto rounded-md px-2 py-1 text-sm",
        before ? "bg-destructive/10" : "bg-success/10",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "select-none font-semibold",
          before ? "text-destructive" : "text-success",
        )}
      >
        {before ? "−" : "+"}
      </span>
      <span className="sr-only">{before ? "Before:" : "After:"}</span>
      <span className="min-w-0 whitespace-pre-wrap break-words">
        {segments.map((segment, index) => {
          if (!segment.changed) return <span key={index}>{segment.text}</span>;
          const Tag = before ? "del" : "ins";
          return (
            <Tag
              key={index}
              className={cn(
                "rounded-sm no-underline",
                before
                  ? "bg-destructive/25 line-through decoration-destructive"
                  : "bg-success/25 font-medium",
              )}
            >
              {segment.text}
            </Tag>
          );
        })}
      </span>
    </div>
  );
}

function ReviewValueChange({ change }: { change: ReviewFieldChange }) {
  const { before, after } = change;
  if (isShortValue(before) && isShortValue(after)) {
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-sm">
        <span className="sr-only">Before:</span>
        {before === null ? (
          <NotSet />
        ) : (
          <span className="break-all rounded-sm bg-destructive/15 px-1 line-through decoration-destructive decoration-2">
            {before}
          </span>
        )}
        <ArrowRightIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="sr-only">After:</span>
        {after === null ? (
          <NotSet />
        ) : (
          <span className="break-all rounded-sm bg-success/15 px-1 font-medium">
            {after}
          </span>
        )}
      </div>
    );
  }
  const words = diffWords(before ?? "", after ?? "");
  return (
    <div className="space-y-1">
      {before === null ? null : (
        <DiffLine tone="before" segments={words.before} />
      )}
      {after === null ? (
        <div className="px-2 text-sm">
          <NotSet />
        </div>
      ) : (
        <DiffLine tone="after" segments={words.after} />
      )}
    </div>
  );
}

function ReviewFieldRow({ change }: { change: ReviewFieldChange }) {
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">{change.label}</div>
      <ReviewValueChange change={change} />
    </div>
  );
}

function ReviewBlockChanges({ review }: { review: BlockReviewAnnotation }) {
  const [expanded, setExpanded] = useState(false);
  if (review.status !== "changed" || review.changes.length === 0) {
    return null;
  }
  const visible = expanded ? review.changes : review.changes.slice(0, 1);
  const hiddenCount = review.changes.length - visible.length;
  return (
    <div className="nodrag nopan cursor-default space-y-3 border-t border-border pt-3 text-left">
      {visible.map((change) => (
        <ReviewFieldRow key={change.key} change={change} />
      ))}
      {review.changes.length > 1 ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {expanded
            ? "Show fewer changes"
            : `+${hiddenCount} more ${hiddenCount === 1 ? "change" : "changes"}`}
        </button>
      ) : null}
    </div>
  );
}

function ReviewStartChanges({ review }: { review: StartReviewAnnotation }) {
  if (review.inputs.length === 0 && review.settings.length === 0) {
    return null;
  }
  return (
    <div className="nodrag nopan mt-3 cursor-default space-y-3 text-left">
      {review.inputs.map((input) => (
        <div key={`input:${input.key}`} className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="w-16 shrink-0 text-xs text-muted-foreground">
              Input
            </span>
            {review.showStatus ? (
              <ReviewStatusChip status={input.status} />
            ) : null}
            <code
              className={cn("font-mono text-sm", {
                "text-muted-foreground line-through":
                  input.status === "removed",
              })}
            >
              {input.key}
            </code>
            {input.detail ? (
              <span className="text-xs text-muted-foreground">
                {input.detail}
              </span>
            ) : null}
          </div>
          {input.changes.length > 0 ? (
            <div className="space-y-2 pl-[4.5rem]">
              {input.changes.map((change) => (
                <ReviewFieldRow key={change.key} change={change} />
              ))}
            </div>
          ) : null}
        </div>
      ))}
      {review.settings.map((setting) => (
        <div key={`setting:${setting.key}`} className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">
              {setting.label}
            </span>
            {review.showStatus ? <ReviewStatusChip status="changed" /> : null}
          </div>
          <ReviewValueChange change={setting} />
        </div>
      ))}
    </div>
  );
}

function ReviewFoldMarker({
  count,
  onShow,
}: {
  count: number;
  onShow: () => void;
}) {
  return (
    <div className="w-[30rem]">
      <Handle type="target" position={Position.Top} className="opacity-0" />
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
      <button
        type="button"
        onClick={onShow}
        className="nodrag nopan flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-slate-elevation2 px-4 py-3 text-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring dark:border-slate-600"
      >
        {count} unchanged {count === 1 ? "block" : "blocks"}
        <span aria-hidden>·</span>
        <span className="font-medium text-foreground">Show</span>
      </button>
    </div>
  );
}

export {
  ReviewBlockChanges,
  ReviewFoldMarker,
  ReviewStartChanges,
  ReviewStatusChip,
};
