import { useReactFlow } from "@xyflow/react";

import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { cn } from "@/util/utils";

import { placeholders } from "../../helpContent";
import {
  blockUrlErrorId,
  useBlockUrlError,
} from "../../hooks/useBlockUrlError";
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
  const urlError = useBlockUrlError(blockId);

  return (
    <div data-testid="url-block-form" className="space-y-4">
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">URL</Label>
        <WorkflowBlockInputTextarea
          name="url"
          nodeId={blockId}
          onChange={(next) => update({ url: next })}
          value={url}
          placeholder={placeholders["url"]["url"]}
          aria-invalid={urlError !== null}
          aria-describedby={urlError ? blockUrlErrorId(blockId) : undefined}
          className={cn(
            "nopan text-xs",
            urlError !== null && "border-destructive",
          )}
        />
        {urlError ? (
          <p id={blockUrlErrorId(blockId)} className="text-xs text-destructive">
            {urlError}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export { URLEditor };
