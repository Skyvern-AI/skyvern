import { Crosshair2Icon, LockClosedIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import { useLocateBlockStore } from "./useLocateBlockStore";
import { useRunValidationStore } from "./useRunValidationStore";

// The only run-blocking rule today is a login block missing a credential.
const BLOCK_REASON = "Login · needs a credential";
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

type ListProps = {
  blocks: Array<RunBlockingBlock>;
  onLocate: (nodeId: string) => void;
  className?: string;
};

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
        <span className="block truncate text-xs text-slate-400">
          {BLOCK_REASON}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-slate-400 group-hover:text-slate-200">
        Locate
        <Crosshair2Icon className="size-3.5" />
      </span>
    </button>
  );
}

function RunBlockingList({ blocks, onLocate, className }: ListProps) {
  return (
    <div className={cn("flex flex-col gap-0.5", className)}>
      {blocks.map((block) => (
        <RunBlockingRow key={block.id} block={block} onLocate={onLocate} />
      ))}
    </div>
  );
}

function blockCountText(count: number): string {
  return `${count} block${count === 1 ? "" : "s"} need${count === 1 ? "s" : ""} fixing`;
}

function RunBlockingPanel({ blocks, onLocate }: ListProps) {
  return (
    <div
      className={cn(
        "absolute left-6 top-[var(--run-blocking-surface-top,8.75rem)] z-50",
        RUN_BLOCKING_PANEL_WIDTH_CLASS,
        "rounded-xl border border-slate-700 bg-slate-elevation3 p-3 shadow-2xl",
      )}
    >
      <div className="px-1 pb-2">
        <p className="text-sm font-semibold text-slate-100">
          {blockCountText(blocks.length)}
        </p>
        <p className="text-xs text-slate-400">
          Resolve these before you can run
        </p>
      </div>
      <RunBlockingList blocks={blocks} onLocate={onLocate} />
    </div>
  );
}

type Props = {
  enabled?: boolean;
};

export function RunBlockingSurface({ enabled = true }: Props = {}) {
  const blocks = useRunValidationStore((state) => state.blockingBlocks);
  const locate = useLocateBlockStore((state) => state.requestLocate);

  if (!enabled || blocks.length === 0) {
    return null;
  }

  return <RunBlockingPanel blocks={blocks} onLocate={locate} />;
}
