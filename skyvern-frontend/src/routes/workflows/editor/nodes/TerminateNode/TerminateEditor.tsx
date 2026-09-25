import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { helpTooltips } from "../../helpContent";
import { useUpdate } from "../../useUpdate";
import { TerminateNode, TerminateNodeData } from "./types";

function TerminateEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<TerminateNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "terminate") {
    return null;
  }
  return <TerminateEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function TerminateEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: TerminateNodeData;
}) {
  const update = useUpdate<TerminateNodeData>({
    id: blockId,
    editable: data.editable,
  });

  return (
    <div data-testid="terminate-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">Reason</Label>
          <HelpTooltip content={helpTooltips["terminate"]["reason"]} />
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(value) => update({ reason: value })}
          value={data.reason}
          placeholder="e.g. ACCOUNT_NOT_FOUND: no account matches {{ account_number }}"
          className="nopan text-xs"
        />
      </div>
    </div>
  );
}

export { TerminateEditor };
