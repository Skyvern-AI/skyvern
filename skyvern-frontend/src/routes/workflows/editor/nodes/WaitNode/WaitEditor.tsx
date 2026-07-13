import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { helpTooltips } from "../../helpContent";
import { WaitNode, WaitNodeData } from "./types";
import { useUpdate } from "../../useUpdate";

function WaitEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount of WaitEditor
  // lives outside the node renderer, so a `useReactFlow().getNode(id)`
  // snapshot would never re-render after `useUpdate` commits typed input
  // and the controlled Input below would revert to the stale value.
  const nodeSlice = useNodesData<WaitNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "wait") {
    return null;
  }
  return <WaitEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function WaitEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: WaitNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<WaitNodeData>({ id: blockId, editable });

  return (
    <div data-testid="wait-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">
            Wait Time (in seconds)
          </Label>
          <HelpTooltip content={helpTooltips["wait"]["waitInSeconds"]} />
        </div>
        <Input
          value={data.waitInSeconds}
          onChange={(event) => {
            update({ waitInSeconds: event.target.value });
          }}
          className="nopan text-xs"
        />
      </div>
    </div>
  );
}

export { WaitEditor };
