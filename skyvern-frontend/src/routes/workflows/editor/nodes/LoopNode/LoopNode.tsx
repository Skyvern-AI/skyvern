import { useEffect, useRef } from "react";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import {
  Handle,
  NodeProps,
  Position,
  useNodes,
  useReactFlow,
  type Node,
} from "@xyflow/react";
import { AppNode } from "..";
import {
  helpTooltips,
  buildWhileLoopConditionTooltip,
} from "../../helpContent";
import { dataSchemaExampleValue } from "../types";
import type { LoopNode } from "./types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { Checkbox } from "@/components/ui/checkbox";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";
import {
  inferBranchCriteriaTypeFromExpression,
  getLoopNodeWidth,
} from "../../workflowEditorUtils";
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
  const headerRef = useRef<HTMLDivElement>(null);
  const { updateNodeData } = useReactFlow();
  const lastHeaderHeight = useRef<number | undefined>(data._headerHeight);

  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;

    const observer = new ResizeObserver(() => {
      // Use offsetHeight to include padding (py-4 = 32px) in the measurement
      const height = Math.round(el.offsetHeight);
      if (lastHeaderHeight.current !== height) {
        lastHeaderHeight.current = height;
        updateNodeData(id, { _headerHeight: height });
        // Trigger re-layout after React processes the data update
        window.dispatchEvent(new Event("loop-header-resized"));
      }
    });

    observer.observe(el);
    return () => observer.disconnect();
  }, [id, updateNodeData]);

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
  const loopKind = data.loopKind;
  const headerBlockType: WorkflowBlockType =
    loopKind === "while" ? "while_loop" : "for_loop";

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
            ref={headerRef}
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
              type={headerBlockType}
            />
            <Tabs
              value={loopKind}
              onValueChange={(v) => {
                if (!data.editable) {
                  return;
                }
                if (v !== "for_each" && v !== "while") {
                  return;
                }
                if (v === "while") {
                  const expr =
                    data.whileConditionExpression.trim() === ""
                      ? "{{ true }}"
                      : data.whileConditionExpression;
                  update({
                    loopKind: "while",
                    whileConditionExpression: expr,
                    whileConditionCriteriaType:
                      inferBranchCriteriaTypeFromExpression(expr),
                  });
                } else {
                  update({ loopKind: "for_each" });
                }
              }}
            >
              <TabsList className="grid h-9 w-full grid-cols-2">
                <TabsTrigger value="for_each" disabled={!data.editable}>
                  For each
                </TabsTrigger>
                <TabsTrigger value="while" disabled={!data.editable}>
                  While
                </TabsTrigger>
              </TabsList>
            </Tabs>
            {loopKind === "for_each" ? (
              <>
                <div className="space-y-2">
                  <div className="flex justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Loop Value
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["loop"]["loopValue"]}
                      />
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
                <WorkflowDataSchemaInputGroup
                  value={data.dataSchema}
                  onChange={(value) => {
                    update({ dataSchema: value });
                  }}
                  suggestionContext={{
                    loop_variable_reference: data.loopVariableReference,
                  }}
                  exampleValue={dataSchemaExampleValue}
                  helpTooltip={helpTooltips["loop"]["loopDataSchema"]}
                />
              </>
            ) : (
              <div className="space-y-2">
                <div className="flex justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Loop Condition
                    </Label>
                    <HelpTooltip content={buildWhileLoopConditionTooltip()} />
                  </div>
                  {isFirstWorkflowBlock ? (
                    <div className="flex justify-end text-xs text-slate-400">
                      Tip: Use the {"+"} button to add parameters!
                    </div>
                  ) : null}
                </div>
                <WorkflowBlockInput
                  nodeId={id}
                  value={data.whileConditionExpression}
                  onChange={(value) => {
                    update({
                      whileConditionExpression: value,
                      whileConditionCriteriaType:
                        inferBranchCriteriaTypeFromExpression(value),
                    });
                  }}
                />
              </div>
            )}
            <div className="space-y-2">
              {loopKind === "for_each" ? (
                <>
                  <div className="flex flex-wrap justify-between gap-x-4 gap-y-3">
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
                      <HelpTooltip
                        content={helpTooltips["loop"]["completeIfEmpty"]}
                      />
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
                        Continue Workflow if Loop Fails
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["loop"]["continueOnFailure"]}
                      />
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-3">
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
                        Skip Iterations that Fail
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["loop"]["nextLoopOnFailure"]}
                      />
                    </div>
                  </div>
                </>
              ) : (
                <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
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
                      Continue Workflow if Loop Fails
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["while_loop"]["continueOnFailure"]}
                    />
                  </div>
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
                      Skip Iterations that Fail
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["while_loop"]["nextLoopOnFailure"]}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
