import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { helpTooltips } from "../../helpContent";
import { type AppNode, isWorkflowBlockNode } from "..";
import type { DownloadNode } from "./types";

function DownloadEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "download") {
    return null;
  }
  return <DownloadEditorBody node={node as DownloadNode} />;
}

function DownloadEditorBody({ node }: { node: DownloadNode }) {
  const { url } = node.data;

  return (
    <div data-testid="download-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-sm text-slate-400">File Path</Label>
          <HelpTooltip content={helpTooltips["download"]["url"]} />
        </div>
        <Input value={url} disabled className="nopan text-xs" />
      </div>
    </div>
  );
}

export { DownloadEditor };
