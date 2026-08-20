import { useReactFlow } from "@xyflow/react";

import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { placeholders } from "../../helpContent";
import { type AppNode, isWorkflowBlockNode } from "..";
import { isUrlNode } from "./types";
import { useUpdate } from "../../useUpdate";

function URLEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isUrlNode(node)) {
    return null;
  }
  return (
    <URLEditorBody
      blockId={blockId}
      url={node.data.url}
      editable={node.data.editable}
    />
  );
}

function URLEditorBody({
  blockId,
  url,
  editable,
}: {
  blockId: string;
  url: string;
  editable: boolean;
}) {
  const update = useUpdate<{ url: string }>({ id: blockId, editable });

  return (
    <div data-testid="url-block-form" className="space-y-4">
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">URL</Label>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(next) => update({ url: next })}
          value={url}
          placeholder={placeholders["url"]["url"]}
          className="nopan text-xs"
        />
      </div>
    </div>
  );
}

export { URLEditor };
