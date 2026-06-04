import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { ErrorCodeMappingEditor } from "../../ErrorCodeMappingEditor";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { type AppNode } from "..";
import { DisableCache } from "../DisableCache";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { type ValidationNode, type ValidationNodeData } from "./types";
import { errorMappingExampleValue } from "../types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

function ValidationEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<ValidationNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "validation") {
    return null;
  }
  return <ValidationEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function ValidationEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: ValidationNodeData;
}) {
  const {
    editable,
    label,
    completeCriterion,
    terminateCriterion,
    errorCodeMapping,
    parameterKeys,
    model,
    continueOnFailure,
    nextLoopOnFailure,
    disableCache,
  } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });
  const update = useUpdate<ValidationNodeData>({ id: blockId, editable });

  return (
    <div data-testid="validation-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <Label className="text-xs text-slate-300">Complete if...</Label>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-slate-400">
              Tip: Use the {"+"} button to add inputs!
            </div>
          ) : null}
        </div>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.validation.completeCriterion}
          nodeId={blockId}
          onChange={(next) => update({ completeCriterion: next })}
          value={completeCriterion}
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Terminate if...</Label>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.validation.terminateCriterion}
          nodeId={blockId}
          onChange={(next) => update({ terminateCriterion: next })}
          value={terminateCriterion}
          className="nopan text-xs"
        />
      </div>
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <div className="space-y-2">
                <ModelSelector
                  className="nopan mr-[1px] w-52 text-xs"
                  value={model}
                  onChange={(next) => update({ model: next })}
                />
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={parameterKeys}
                  onParametersChange={(next) => update({ parameterKeys: next })}
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Error Messages
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["validation"]["errorCodeMapping"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={errorCodeMapping !== "null"}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        update({
                          errorCodeMapping: checked
                            ? JSON.stringify(errorMappingExampleValue, null, 2)
                            : "null",
                        });
                      }}
                    />
                  </div>
                </div>
                {errorCodeMapping !== "null" && (
                  <ErrorCodeMappingEditor
                    label={label}
                    value={errorCodeMapping}
                    onChange={(next) => update({ errorCodeMapping: next })}
                    readOnly={!editable}
                  />
                )}
              </div>
              <BlockExecutionOptions
                continueOnFailure={continueOnFailure}
                nextLoopOnFailure={nextLoopOnFailure}
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="validation"
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
                }
              />
              <DisableCache
                disableCache={disableCache}
                editable={editable}
                onDisableCacheChange={(next) => update({ disableCache: next })}
              />
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { ValidationEditor };
