import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { helpTooltips } from "../../helpContent";
import { type AppNode, isWorkflowBlockNode } from "..";
import type { UploadNode } from "./types";

function UploadEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "upload") {
    return null;
  }
  return <UploadEditorBody node={node as UploadNode} />;
}

function UploadEditorBody({ node }: { node: UploadNode }) {
  const { path } = node.data;

  return (
    <div data-testid="upload-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-sm text-slate-400">File Path</Label>
          <HelpTooltip content={helpTooltips["upload"]["path"]} />
        </div>
        <Input value={path} disabled className="nopan text-xs" />
      </div>
    </div>
  );
}

export { UploadEditor };
