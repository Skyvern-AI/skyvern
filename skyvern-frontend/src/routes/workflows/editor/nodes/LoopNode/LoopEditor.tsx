import { useNodesData } from "@xyflow/react";

import { Checkbox } from "@/components/ui/checkbox";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import {
  buildWhileLoopConditionTooltip,
  helpTooltips,
} from "../../helpContent";
import { inferBranchCriteriaTypeFromExpression } from "../../workflowEditorUtils";
import { type LoopNode, type LoopNodeData } from "./types";
import { dataSchemaExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";

function LoopEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<LoopNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "loop") return null;
  return <LoopEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function LoopEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: LoopNodeData;
}) {
  const {
    editable,
    loopKind,
    loopVariableReference,
    dataSchema,
    completeIfEmpty,
    continueOnFailure,
    nextLoopOnFailure,
    whileConditionExpression,
  } = data;
  const update = useUpdate<LoopNodeData>({ id: blockId, editable });

  const setBool =
    (key: keyof LoopNodeData) => (checked: boolean | "indeterminate") =>
      update({
        [key]: checked === "indeterminate" ? false : checked,
      } as Partial<LoopNodeData>);

  return (
    <div data-testid="loop-block-form" className="space-y-4">
      <Tabs
        value={loopKind}
        onValueChange={(v) => {
          if (!editable) {
            return;
          }
          if (v !== "for_each" && v !== "while") {
            return;
          }
          if (v === "while") {
            const expr =
              whileConditionExpression.trim() === ""
                ? "{{ true }}"
                : whileConditionExpression;
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
          <TabsTrigger value="for_each" disabled={!editable}>
            For each
          </TabsTrigger>
          <TabsTrigger value="while" disabled={!editable}>
            While
          </TabsTrigger>
        </TabsList>
      </Tabs>

      {loopKind === "for_each" ? (
        <>
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Loop Value</Label>
              <HelpTooltip content={helpTooltips["loop"]["loopValue"]} />
            </div>
            <WorkflowBlockInput
              nodeId={blockId}
              value={loopVariableReference}
              onChange={(v) => update({ loopVariableReference: v })}
              data-testid="loop-variable-input"
            />
          </div>

          <WorkflowDataSchemaInputGroup
            value={dataSchema}
            onChange={(v) => update({ dataSchema: v })}
            suggestionContext={{
              loop_variable_reference: loopVariableReference,
            }}
            exampleValue={dataSchemaExampleValue}
            helpTooltip="Specify a format for extracted data in JSON. Only applies when the loop value is natural language - ignored for input references."
          />

          <div className="flex items-center gap-2">
            <Checkbox
              checked={completeIfEmpty}
              disabled={!editable}
              onCheckedChange={setBool("completeIfEmpty")}
              data-testid="checkbox-completeIfEmpty"
            />
            <Label className="text-xs text-slate-300">Continue if Empty</Label>
            <HelpTooltip content="When checked, the for loop block will successfully complete and workflow execution will continue if the loop value is empty" />
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              checked={continueOnFailure}
              disabled={!editable}
              onCheckedChange={setBool("continueOnFailure")}
              data-testid="checkbox-continueOnFailure"
            />
            <Label className="text-xs text-slate-300">
              Continue Workflow if Loop Fails
            </Label>
            <HelpTooltip content={helpTooltips["loop"]["continueOnFailure"]} />
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              checked={nextLoopOnFailure ?? false}
              disabled={!editable}
              onCheckedChange={setBool("nextLoopOnFailure")}
              data-testid="checkbox-nextLoopOnFailure"
            />
            <Label className="text-xs text-slate-300">
              Skip Iterations that Fail
            </Label>
            <HelpTooltip content={helpTooltips["loop"]["nextLoopOnFailure"]} />
          </div>
        </>
      ) : (
        <>
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Loop Condition</Label>
              <HelpTooltip content={buildWhileLoopConditionTooltip()} />
            </div>
            <WorkflowBlockInput
              nodeId={blockId}
              value={whileConditionExpression}
              onChange={(v) =>
                update({
                  whileConditionExpression: v,
                  whileConditionCriteriaType:
                    inferBranchCriteriaTypeFromExpression(v),
                })
              }
              data-testid="while-condition-input"
            />
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              checked={continueOnFailure}
              disabled={!editable}
              onCheckedChange={setBool("continueOnFailure")}
              data-testid="checkbox-continueOnFailure"
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
              checked={nextLoopOnFailure ?? false}
              disabled={!editable}
              onCheckedChange={setBool("nextLoopOnFailure")}
              data-testid="checkbox-nextLoopOnFailure"
            />
            <Label className="text-xs text-slate-300">
              Skip Iterations that Fail
            </Label>
            <HelpTooltip
              content={helpTooltips["while_loop"]["nextLoopOnFailure"]}
            />
          </div>
        </>
      )}
    </div>
  );
}

export { LoopEditor };
