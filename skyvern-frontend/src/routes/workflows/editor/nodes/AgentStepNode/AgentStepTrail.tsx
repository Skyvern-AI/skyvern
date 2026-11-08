import { useReactFlow } from "@xyflow/react";
import type { MouseEvent, ReactNode } from "react";

import type { Status } from "@/api/types";
import {
  toolCallSummary,
  toolCallTitle,
} from "@/routes/workflows/studio/agentStepTrail";
import {
  useAgentStepTrail,
  useSelectAgentStepCall,
  useSelectedAgentStepCall,
} from "@/routes/workflows/studio/useAgentStepTrail";
import { StatusDot } from "@/routes/workflows/workflowRun/StatusDot";
import { cn } from "@/util/utils";

import { ToolCallIcon } from "./ToolCallIcon";

// React Flow's own edge colour, so the connectors read as the canvas's edges.
const EDGE_STYLE = {
  backgroundColor: "var(--xy-edge-stroke, var(--xy-edge-stroke-default))",
};

// The call panel's 30rem width plus its 1.5rem right inset and a 1.5rem gap.
const CALL_PANEL_COVER_PX = 33 * 16;

function TrailCard({
  number,
  icon,
  title,
  summary,
  status,
  running,
  selected,
  onSelect,
}: {
  number: number;
  icon: ReactNode;
  title: string;
  summary: string;
  status: Status;
  running: boolean;
  selected: boolean;
  onSelect: (card: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        event.stopPropagation();
        onSelect(event.currentTarget);
      }}
      aria-pressed={selected}
      className={cn(
        "flex w-full items-center gap-3 rounded-lg bg-slate-elevation3 px-4 py-3 text-left shadow-sm focus-visible:outline-dashed focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring",
        {
          "bg-background outline outline-2 outline-ring": running,
          "ring-2 ring-ring ring-offset-2 ring-offset-background": selected,
        },
      )}
    >
      <div className="flex h-[2.75rem] w-[2.75rem] shrink-0 items-center justify-center rounded border border-border dark:border-slate-600">
        {icon}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="truncate text-sm">{`#${number} ${title}`}</span>
        {summary && (
          <span className="truncate text-xs text-muted-foreground">
            {summary}
          </span>
        )}
      </div>
      <StatusDot status={status} isFinalized={!running} />
    </button>
  );
}

function AgentStepTrail({ agentStepLabel }: { agentStepLabel: string }) {
  const { getViewport, setViewport } = useReactFlow();
  // The call panel covers the pane's right edge, so pan the selected card into view to its left.
  const revealLeftOfPanel = (card: HTMLElement) => {
    const pane = card.closest(".react-flow");
    if (!pane) {
      return;
    }
    const overflow =
      card.getBoundingClientRect().right -
      (pane.getBoundingClientRect().right - CALL_PANEL_COVER_PX);
    if (overflow > 0) {
      const viewport = getViewport();
      void setViewport(
        { ...viewport, x: viewport.x - overflow },
        { duration: 200 },
      );
    }
  };
  const trail = useAgentStepTrail({ label: agentStepLabel });
  const selectCall = useSelectAgentStepCall();
  const selected = useSelectedAgentStepCall();
  if (!trail || trail.entries.length === 0) {
    return null;
  }
  return (
    <div
      data-testid="agent-step-trail"
      className="nodrag nopan absolute left-full top-0 ml-16 flex w-72 flex-col items-center"
      onPointerDown={(event) => event.stopPropagation()}
    >
      <div
        className="absolute -left-16 top-[2.375rem] h-px w-16"
        style={EDGE_STYLE}
      />
      {trail.entries.map((entry, position) => (
        <div
          key={entry.call.index}
          className="flex w-full flex-col items-center"
        >
          {position > 0 && <div className="h-6 w-px" style={EDGE_STYLE} />}
          <TrailCard
            number={entry.call.index}
            icon={
              <ToolCallIcon
                tool={entry.call.tool}
                blockType={entry.call.block_type}
                className="size-6"
              />
            }
            title={toolCallTitle(entry.call)}
            summary={toolCallSummary(entry.call)}
            status={entry.status}
            running={entry.running}
            selected={
              selected?.workflowRunBlockId ===
                trail.item.block.workflow_run_block_id &&
              selected.index === entry.call.index
            }
            onSelect={(card) => {
              selectCall(trail.item.block, entry);
              revealLeftOfPanel(card);
            }}
          />
        </div>
      ))}
    </div>
  );
}

export { AgentStepTrail };
