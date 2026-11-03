import {
  ChevronDownIcon,
  ChevronRightIcon,
  Crosshair2Icon,
  DoubleArrowUpIcon,
  LockClosedIcon,
} from "@radix-ui/react-icons";
import { Fragment, useEffect, useRef } from "react";

import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
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
export const RUN_BLOCKING_SURFACE_BOTTOM_VAR = "--run-blocking-surface-bottom";
export const WORKFLOW_EDITOR_HEADER_TOP = "2rem";
export const WORKFLOW_EDITOR_HEADER_HEIGHT = "5rem";
export const RUN_BLOCKING_SURFACE_TOP = `calc(${[
  `var(${WORKFLOW_EDITOR_HEADER_TOP_VAR})`,
  `var(${WORKFLOW_EDITOR_HEADER_HEIGHT_VAR})`,
  RUN_BLOCKING_PANEL_GAP_BELOW_HEADER,
].join(" + ")})`;

const ANCHOR_CLASS =
  "absolute left-6 top-[var(--run-blocking-surface-top,8.75rem)] z-50";
// Over the Doc the surface sits in a pane that can be narrower than the panel, and follows the light theme.
const DOC_ANCHOR_CLASS = "left-3 right-3 w-auto max-w-[19rem]";

// Publishes where the banner ends on its parent so full-bleed views (the Doc) can start below it.
function useReportSurfaceBottom<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    const element = ref.current;
    const parent = element?.parentElement;
    if (!element || !parent) return;
    const report = () =>
      parent.style.setProperty(
        RUN_BLOCKING_SURFACE_BOTTOM_VAR,
        `${element.offsetTop + element.offsetHeight + 12}px`,
      );
    report();
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(report);
    observer?.observe(element);
    return () => {
      observer?.disconnect();
      parent.style.removeProperty(RUN_BLOCKING_SURFACE_BOTTOM_VAR);
    };
  }, []);
  return ref;
}

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
  doc,
}: {
  path: Array<RunBlockingPathSegment>;
  doc: boolean;
}) {
  if (path.length === 0) {
    return null;
  }
  return (
    <span
      className={
        doc
          ? "flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[0.7rem] leading-tight text-muted-foreground"
          : "flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[0.7rem] leading-tight text-slate-500"
      }
    >
      {path.map((segment, index) => (
        <Fragment key={`${segment.kind}-${segment.label}-${index}`}>
          {index > 0 ? (
            <ChevronRightIcon className="size-3 shrink-0 opacity-60" />
          ) : null}
          <span className={doc ? "[overflow-wrap:anywhere]" : "truncate"}>
            {segmentText(segment)}
          </span>
        </Fragment>
      ))}
    </span>
  );
}

function RunBlockingRow({
  block,
  onLocate,
  doc,
}: {
  block: RunBlockingBlock;
  onLocate: (nodeId: string) => void;
  doc: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onLocate(block.id)}
      className={
        doc
          ? "group flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-500"
          : "group flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-slate-elevation5 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400"
      }
    >
      <span
        className={
          doc
            ? "flex size-7 shrink-0 items-center justify-center rounded-md border border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-400"
            : "flex size-7 shrink-0 items-center justify-center rounded-md border border-amber-500/40 bg-amber-500/15 text-amber-400"
        }
      >
        <LockClosedIcon className="size-3.5" />
      </span>
      <span className={doc ? "min-w-0 flex-1 basis-32" : "min-w-0 flex-1"}>
        <span
          className={
            doc
              ? "block text-sm font-medium text-foreground [overflow-wrap:anywhere]"
              : "block truncate text-sm font-medium text-slate-50"
          }
        >
          {block.label}
        </span>
        <BlockPathBreadcrumb path={block.path} doc={doc} />
        <span
          className={
            doc
              ? "block text-xs text-muted-foreground"
              : "block truncate text-xs text-slate-400"
          }
        >
          {RUN_BLOCKING_REASON}
        </span>
      </span>
      <span
        className={
          doc
            ? "ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground group-hover:text-foreground"
            : "flex shrink-0 items-center gap-1 text-xs text-slate-400 group-hover:text-slate-200"
        }
      >
        Locate
        <Crosshair2Icon className="size-3.5" />
      </span>
    </button>
  );
}

function RunBlockingPill({
  count,
  onExpand,
  doc,
}: {
  count: number;
  onExpand: () => void;
  doc: boolean;
}) {
  const reportBottom = useReportSurfaceBottom<HTMLButtonElement>();
  return (
    <button
      ref={reportBottom}
      type="button"
      onClick={onExpand}
      aria-expanded={false}
      className={cn(
        ANCHOR_CLASS,
        "flex items-center gap-2 rounded-full border border-amber-500/40 bg-slate-elevation3 py-1.5 pl-3 pr-2.5 text-sm font-semibold text-amber-200 shadow-lg transition-colors hover:bg-slate-elevation4 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400",
        "duration-200 animate-in fade-in slide-in-from-left-2",
        doc &&
          "left-3 max-w-[calc(100%-1.5rem)] bg-popover text-amber-800 hover:bg-muted dark:text-amber-200",
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
  doc,
}: {
  blocks: Array<RunBlockingBlock>;
  onLocate: (nodeId: string) => void;
  onCollapse: () => void;
  doc: boolean;
}) {
  const reportBottom = useReportSurfaceBottom<HTMLDivElement>();
  return (
    <div
      ref={reportBottom}
      className={cn(
        ANCHOR_CLASS,
        RUN_BLOCKING_PANEL_WIDTH_CLASS,
        "rounded-xl border border-slate-700 bg-slate-elevation3 p-3 shadow-2xl",
        "duration-200 animate-in fade-in slide-in-from-left-2",
        doc && [DOC_ANCHOR_CLASS, "border-border bg-popover"],
      )}
    >
      <div className="flex items-start gap-2 px-1 pb-2">
        <div className="min-w-0 flex-1">
          <p
            className={
              doc
                ? "text-sm font-semibold text-foreground"
                : "text-sm font-semibold text-slate-100"
            }
          >
            {blockCountText(blocks.length)}
          </p>
          <p
            className={
              doc ? "text-xs text-muted-foreground" : "text-xs text-slate-400"
            }
          >
            Resolve these before you can run
          </p>
        </div>
        <button
          type="button"
          onClick={onCollapse}
          aria-label="Collapse run-blocking panel"
          className={
            doc
              ? "flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-500"
              : "flex size-6 shrink-0 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-elevation5 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-400"
          }
        >
          <DoubleArrowUpIcon className="size-3.5" />
        </button>
      </div>
      <div className="flex max-h-[calc(100vh-16rem)] flex-col gap-0.5 overflow-y-auto">
        {blocks.map((block) => (
          <RunBlockingRow
            key={block.id}
            block={block}
            onLocate={onLocate}
            doc={doc}
          />
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
  const yamlActive = useWorkflowYamlEditorStore((state) => state.active);
  const doc = useWorkflowPresentationStore(
    (state) => state.enabled && state.presentation === "doc" && !yamlActive,
  );

  if (blocks.length === 0) {
    return null;
  }

  if (collapsed) {
    return (
      <RunBlockingPill
        count={blocks.length}
        onExpand={() => setCollapsed(false)}
        doc={doc}
      />
    );
  }

  return (
    <RunBlockingPanel
      blocks={blocks}
      onLocate={locate}
      onCollapse={() => setCollapsed(true)}
      doc={doc}
    />
  );
}
