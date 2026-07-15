import { Handle, NodeProps, Position } from "@xyflow/react";
import { useParams } from "react-router-dom";

import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";

import { useCollapseContext } from "../../collapse/CollapseContext";
import { NodeBody } from "../../collapse/NodeBody";
import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { SendEmailEditor } from "./SendEmailEditor";
import { type SendEmailNode } from "./types";

function SendEmailNode({ id, data }: NodeProps<SendEmailNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const recordingStore = useRecordingStore();
  const { open } = useCollapseContext();

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
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow motion-reduce:transition-none",
          open ? "shadow-md" : "shadow-sm",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-background outline outline-2 outline-ring":
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
          type="send_email"
        />
        <NodeBody>
          <BuildModeOnly>
            <SendEmailEditor blockId={id} />
          </BuildModeOnly>
        </NodeBody>
      </div>
    </div>
  );
}

export { SendEmailNode };
