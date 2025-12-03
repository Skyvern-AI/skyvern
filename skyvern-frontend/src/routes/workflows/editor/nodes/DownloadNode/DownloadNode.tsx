import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import type { DownloadNode } from "./types";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRecordingStore } from "@/store/useRecordingStore";

function DownloadNode({ id, data }: NodeProps<DownloadNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const recordingStore = useRecordingStore();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
    >
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="file_download" // sic: the naming is not consistent
        />
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">File Path</Label>
              <HelpTooltip content={helpTooltips["download"]["url"]} />
            </div>
            <Input value={data.url} disabled className="nopan text-xs" />
          </div>
        </div>
      </div>
    </div>
  );
}

export { DownloadNode };
