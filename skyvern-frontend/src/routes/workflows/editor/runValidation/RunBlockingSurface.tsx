import {
  ChevronDownIcon,
  ChevronRightIcon,
  Crosshair2Icon,
  DoubleArrowUpIcon,
  LockClosedIcon,
} from "@radix-ui/react-icons";
import { Fragment } from "react";

import { cn } from "@/util/utils";

import {
  RUN_BLOCKING_REASON,
  type RunBlockingBlock,
} from "./getRunBlockingBlocks";
import type { RunBlockingPathSegment } from "./resolveBlockPath";
import { useLocateBlockStore } from "./useLocateBlockStore";
import { useRunBlockingPanelStore } from "./useRunBlockingPanelStore";
import { useRunValidationStore } from "./useRunValidationStore";

const RUN_BLOCKING_PANEL_GAP_BELOW_HEADER = "1.75rem";
const RUN_BLOCKING_PANEL_WIDTH_CLASS = "w-[19rem]";

export const WORKFLOW_EDITOR_HEADER_TOP_VAR = "--workflow-editor-header-top";
export const WORKFLOW_EDITOR_HEADER_HEIGHT_VAR =
  "--workflow-editor-header-height";
export const RUN_BLOCKING_SURFACE_TOP_VAR = "--run-blocking-surface-top";
export const WORKFLOW_EDITOR_HEADER_TOP = "2rem";
export const WORKFLOW_EDITOR_HEADER_HEIGHT = "5rem";
export const RUN_BLOCKING_SURFACE_TOP = `calc(${[
  `var(${WORKFLOW_EDITOR_HEADER_TOP_VAR})`,
  `var(${WORKFLOW_EDITOR_HEADER_HEIGHT_VAR})`,
  RUN_BLOCKING_PANEL_GAP_BELOW_HEADER,
].join(" + ")})`;

const ANCHOR_CLASS =
  "absolute left-6 top-[var(--run-blocking-surface-top,8.75rem)] z-50";

function blockCountText(count: number): string {
  return `${count} block${count === 1 ? "" : "s"} need${count === 1 ? "s" : ""} fixing`;
}

function segmentText(segment: RunBlockingPathSegment): string {
  if (segment.kind === "conditional" && segment.branch) {
    return `${segment.label} · ${segment.branch}`;
  }
  return segment.label;
}

function BlockPathBreadcrumb({
  path,
}: {
  path: Array<RunBlockingPathSegment>;
}) {
  if (path.length === 0) {
    return null;
  }
  return (
    <span className="flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[0.7rem] leading-tight text-slate-500">
      {path.map((segment, index) => (
        <Fragment key={`${segment.kind}-${segment.label}-${index}`}>
          {index > 0 ? (
            <ChevronRightIcon className="size-3 shrink-0 opacity-60" />
          ) : null}
          <span className="truncate">{segmentText(segment)}</span>
        </Fragment>
      ))}
    </span>
  );
}

function RunBlockingRow({
  block,
  onLocate,
}: {
  block: RunBlockingBlock;
  onLocate: (nodeId: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onLocate(block.id)}
      className="group flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-slate-elevation5 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400"
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-amber-500/40 bg-amber-500/15 text-amber-400">
        <LockClosedIcon className="size-3.5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-slate-50">
          {block.label}
        </span>
        <BlockPathBreadcrumb path={block.path} />
        <span className="block truncate text-xs text-slate-400">
          {RUN_BLOCKING_REASON}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-slate-400 group-hover:text-slate-200">
        Locate
        <Crosshair2Icon className="size-3.5" />
      </span>
    </button>
  );
}

function RunBlockingPill({
  count,
  onExpand,
}: {
  count: number;
  onExpand: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onExpand}
      aria-expanded={false}
      className={cn(
        ANCHOR_CLASS,
        "flex items-center gap-2 rounded-full border border-amber-500/40 bg-slate-elevation3 py-1.5 pl-3 pr-2.5 text-sm font-semibold text-amber-200 shadow-lg transition-colors hover:bg-slate-elevation4 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400",
        "duration-200 animate-in fade-in slide-in-from-left-2",
      )}
    >
      <span className="flex size-2 shrink-0 rounded-full bg-amber-400" />
      {blockCountText(count)}
      <ChevronDownIcon className="size-4 opacity-80" />
    </button>
  );
}

function RunBlockingPanel({
  blocks,
  onLocate,
  onCollapse,
}: {
  blocks: Array<RunBlockingBlock>;
  onLocate: (nodeId: string) => void;
  onCollapse: () => void;
}) {
  return (
    <div
      className={cn(
        ANCHOR_CLASS,
        RUN_BLOCKING_PANEL_WIDTH_CLASS,
        "rounded-xl border border-slate-700 bg-slate-elevation3 p-3 shadow-2xl",
        "duration-200 animate-in fade-in slide-in-from-left-2",
      )}
    >
      <div className="flex items-start gap-2 px-1 pb-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-slate-100">
            {blockCountText(blocks.length)}
          </p>
          <p className="text-xs text-slate-400">
            Resolve these before you can run
          </p>
        </div>
        <button
          type="button"
          onClick={onCollapse}
          aria-label="Collapse run-blocking panel"
          className="flex size-6 shrink-0 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-elevation5 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400"
        >
          <DoubleArrowUpIcon className="size-3.5" />
        </button>
      </div>
      <div className="flex max-h-[calc(100vh-16rem)] flex-col gap-0.5 overflow-y-auto">
        {blocks.map((block) => (
          <RunBlockingRow key={block.id} block={block} onLocate={onLocate} />
        ))}
      </div>
    </div>
  );
}

export function RunBlockingSurface() {
  const blocks = useRunValidationStore((state) => state.blockingBlocks);
  const locate = useLocateBlockStore((state) => state.requestLocate);
  const collapsed = useRunBlockingPanelStore((state) => state.collapsed);
  const setCollapsed = useRunBlockingPanelStore((state) => state.setCollapsed);

  if (blocks.length === 0) {
    return null;
  }

  if (collapsed) {
    return (
      <RunBlockingPill
        count={blocks.length}
        onExpand={() => setCollapsed(false)}
      />
    );
  }

  return (
    <RunBlockingPanel
      blocks={blocks}
      onLocate={locate}
      onCollapse={() => setCollapsed(true)}
    />
  );
}
