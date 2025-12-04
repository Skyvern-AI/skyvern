import { useEffect } from "react";
import { Flippable } from "@/components/Flippable";
import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import { errorMappingExampleValue } from "../types";
import type { ValidationNode } from "./types";
import { AppNode } from "..";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { ModelSelector } from "@/components/ModelSelector";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRerender } from "@/hooks/useRerender";

import { DisableCache } from "../DisableCache";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { AI_IMPROVE_CONFIGS } from "../../constants";

function ValidationNode({ id, data, type }: NodeProps<ValidationNode>) {
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const { editable, label } = data;
  const script = blockScriptStore.scripts[label];
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const rerender = useRerender({ prefix: "accordian" });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const update = useUpdate<ValidationNode["data"]>({ id, editable });
  const isInsideForLoop = isNodeInsideForLoop(nodes, id);

  useEffect(() => {
    setFacing(data.showCode ? "back" : "front");
  }, [data.showCode]);

  return (
    <Flippable facing={facing} preserveFrontsideHeight={true}>
      <div>
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
            data.comparisonColor,
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={null}
            totpUrl={null}
            transmutations={{
              blockTitle: "Validation",
              self: "agent",
              others: [
                {
                  label: "human",
                  reason: "Convert to human validation",
                  nodeName: "human_interaction",
                },
              ],
            }}
            type={type}
          />
          <div className="space-y-2">
            <div className="flex justify-between">
              <Label className="text-xs text-slate-300">Complete if...</Label>
              {isFirstWorkflowBlock ? (
                <div className="flex justify-end text-xs text-slate-400">
                  Tip: Use the {"+"} button to add parameters!
                </div>
              ) : null}
            </div>
            <WorkflowBlockInputTextarea
              aiImprove={AI_IMPROVE_CONFIGS.validation.completeCriterion}
              nodeId={id}
              onChange={(value) => {
                update({ completeCriterion: value });
              }}
              value={data.completeCriterion}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <Label className="text-xs text-slate-300">Terminate if...</Label>
            <WorkflowBlockInputTextarea
              aiImprove={AI_IMPROVE_CONFIGS.validation.terminateCriterion}
              nodeId={id}
              onChange={(value) => {
                update({ terminateCriterion: value });
              }}
              value={data.terminateCriterion}
              className="nopan text-xs"
            />
          </div>
          <Separator />
          <Accordion
            type="single"
            collapsible
            onValueChange={() => rerender.bump()}
          >
            <AccordionItem value="advanced" className="border-b-0">
              <AccordionTrigger className="py-0">
                Advanced Settings
              </AccordionTrigger>
              <AccordionContent>
                <div key={rerender.key} className="ml-6 mt-4 space-y-4">
                  <div className="space-y-2">
                    <ModelSelector
                      className="nopan mr-[1px] w-52 text-xs"
                      value={data.model}
                      onChange={(value) => {
                        update({ model: value });
                      }}
                    />
                    <ParametersMultiSelect
                      availableOutputParameters={outputParameterKeys}
                      parameters={data.parameterKeys}
                      onParametersChange={(parameterKeys) => {
                        update({ parameterKeys });
                      }}
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex gap-4">
                      <div className="flex gap-2">
                        <Label className="text-xs font-normal text-slate-300">
                          Error Messages
                        </Label>
                        <HelpTooltip
                          content={
                            helpTooltips["validation"]["errorCodeMapping"]
                          }
                        />
                      </div>
                      <Checkbox
                        checked={data.errorCodeMapping !== "null"}
                        disabled={!editable}
                        onCheckedChange={(checked) => {
                          if (!editable) {
                            return;
                          }
                          update({
                            errorCodeMapping: checked
                              ? JSON.stringify(
                                  errorMappingExampleValue,
                                  null,
                                  2,
                                )
                              : "null",
                          });
                        }}
                      />
                    </div>
                    {data.errorCodeMapping !== "null" && (
                      <div>
                        <CodeEditor
                          language="json"
                          value={data.errorCodeMapping}
                          onChange={(value) => {
                            if (!editable) {
                              return;
                            }
                            update({ errorCodeMapping: value });
                          }}
                          className="nopan"
                          fontSize={8}
                        />
                      </div>
                    )}
                  </div>
                  <BlockExecutionOptions
                    continueOnFailure={data.continueOnFailure}
                    nextLoopOnFailure={data.nextLoopOnFailure}
                    editable={editable}
                    isInsideForLoop={isInsideForLoop}
                    blockType="validation"
                    onContinueOnFailureChange={(checked) => {
                      update({ continueOnFailure: checked });
                    }}
                    onNextLoopOnFailureChange={(checked) => {
                      update({ nextLoopOnFailure: checked });
                    }}
                  />
                  <DisableCache
                    disableCache={data.disableCache}
                    editable={editable}
                    onDisableCacheChange={(disableCache) => {
                      update({ disableCache });
                    }}
                  />
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
      <BlockCodeEditor blockLabel={label} blockType={type} script={script} />
    </Flippable>
  );
}

export { ValidationNode };
