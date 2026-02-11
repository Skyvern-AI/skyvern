import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import type { Node } from "@xyflow/react";
import { Handle, NodeProps, Position, useNodes } from "@xyflow/react";
import { AppNode } from "..";
import { helpTooltips } from "../../helpContent";
import type { LoopNode } from "./types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { Checkbox } from "@/components/ui/checkbox";
import { getLoopNodeWidth } from "../../workflowEditorUtils";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRecordingStore } from "@/store/useRecordingStore";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const nodes = useNodes<AppNode>();
  const node = nodes.find((n) => n.id === id);
  if (!node) {
    throw new Error("Node not found"); // not possible
  }
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const update = useUpdate<LoopNode["data"]>({ id, editable });
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const children = nodes.filter((node) => node.parentId === id);
  const recordingStore = useRecordingStore();

  const furthestDownChild: Node | null = children.reduce(
    (acc, child) => {
      if (!acc) {
        return child;
      }
      if (child.position.y > acc.position.y) {
        return child;
      }
      return acc;
    },
    null as Node | null,
  );

  const childrenHeightExtent =
    (furthestDownChild?.measured?.height ?? 0) +
    (furthestDownChild?.position.y ?? 0) +
    24;

  const loopNodeWidth = getLoopNodeWidth(node, nodes);

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
        className="rounded-xl border-2 border-dashed border-slate-600 p-2"
        style={{
          width: loopNodeWidth,
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div
            className={cn(
              "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
              {
                "pointer-events-none": thisBlockIsPlaying,
                "bg-slate-950 outline outline-2 outline-slate-300":
                  thisBlockIsTargetted,
              },
              data.comparisonColor,
            )}
          >
            <NodeHeader
              blockLabel={label}
              editable={editable}
              nodeId={id}
              totpIdentifier={null}
              totpUrl={null}
              type="for_loop" // sic: the naming is not consistent
            />
            <div className="space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Loop Value</Label>
                  <HelpTooltip content={helpTooltips["loop"]["loopValue"]} />
                </div>
                {isFirstWorkflowBlock ? (
                  <div className="flex justify-end text-xs text-slate-400">
                    Tip: Use the {"+"} button to add parameters!
                  </div>
                ) : null}
              </div>
              <WorkflowBlockInput
                nodeId={id}
                value={data.loopVariableReference}
                onChange={(value) => {
                  update({ loopVariableReference: value });
                }}
              />
            </div>
            <div className="space-y-2">
              <div className="space-y-2">
                <div className="flex justify-between">
                  <div className="flex items-center gap-2">
                    <Checkbox
                      checked={data.completeIfEmpty}
                      disabled={!data.editable}
                      onCheckedChange={(checked) => {
                        update({
                          completeIfEmpty:
                            checked === "indeterminate" ? false : checked,
                        });
                      }}
                    />
                    <Label className="text-xs text-slate-300">
                      Continue if Empty
                    </Label>
                    <HelpTooltip content="When checked, the for loop block will successfully complete and workflow execution will continue if the loop value is empty" />
                  </div>

                  <div className="flex items-center gap-2">
                    <Checkbox
                      checked={data.continueOnFailure}
                      disabled={!data.editable}
                      onCheckedChange={(checked) => {
                        update({
                          continueOnFailure:
                            checked === "indeterminate" ? false : checked,
                        });
                      }}
                    />
                    <Label className="text-xs text-slate-300">
                      Continue on Failure
                    </Label>
                    <HelpTooltip content="When checked, the loop will continue executing even if one of its iterations fails" />
                  </div>
                </div>
                <div className="flex justify-between">
                  <div className="flex items-center gap-2">
                    <Checkbox
                      checked={data.nextLoopOnFailure ?? false}
                      disabled={!data.editable}
                      onCheckedChange={(checked) => {
                        update({
                          nextLoopOnFailure:
                            checked === "indeterminate" ? false : checked,
                        });
                      }}
                    />
                    <Label className="text-xs text-slate-300">
                      Next Loop on Failure
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["loop"]["nextLoopOnFailure"]}
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
