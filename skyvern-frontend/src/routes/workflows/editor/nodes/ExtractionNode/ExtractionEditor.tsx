import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { type AppNode } from "..";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import type { ExtractionNode, ExtractionNodeData } from "./types";
import { dataSchemaExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

function ExtractionEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<ExtractionNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "extraction") {
    return null;
  }
  return <ExtractionEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function ExtractionEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: ExtractionNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<ExtractionNodeData>({ id: blockId, editable });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);

  return (
    <div
      data-testid="extraction-block-form"
      data-block-id={blockId}
      className="space-y-4"
    >
      <Accordion type="multiple" defaultValue={["extraction"]}>
        <AccordionItem value="extraction">
          <AccordionTrigger>Extraction</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="space-y-2">
                <div className="flex justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Data Extraction Goal
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["extraction"]["dataExtractionGoal"]}
                    />
                  </div>
                  {isFirstWorkflowBlock ? (
                    <div className="flex justify-end text-xs text-slate-400">
                      Tip: Use the {"+"} button to add inputs!
                    </div>
                  ) : null}
                </div>
                <WorkflowBlockInputTextarea
                  aiImprove={{
                    useCase:
                      AI_IMPROVE_CONFIGS.extraction.dataExtractionGoal.useCase,
                    context: {
                      ...AI_IMPROVE_CONFIGS.extraction.dataExtractionGoal
                        .context,
                      data_schema:
                        data.dataSchema && data.dataSchema !== "null"
                          ? data.dataSchema
                          : undefined,
                    },
                  }}
                  nodeId={blockId}
                  onChange={(value) => {
                    if (!editable) return;
                    update({ dataExtractionGoal: value });
                  }}
                  value={data.dataExtractionGoal}
                  placeholder={placeholders["extraction"]["dataExtractionGoal"]}
                  className="nopan text-xs"
                />
              </div>
              <WorkflowDataSchemaInputGroup
                value={data.dataSchema}
                onChange={(value) => update({ dataSchema: value })}
                exampleValue={dataSchemaExampleValue}
                suggestionContext={{
                  data_extraction_goal: data.dataExtractionGoal,
                  current_schema: data.dataSchema,
                }}
              />
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger>Advanced Settings</AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <div className="space-y-2">
                <ModelSelector
                  className="nopan w-52 text-xs"
                  value={data.model}
                  onChange={(value) => update({ model: value })}
                />
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) =>
                    update({ parameterKeys })
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Engine
                  </Label>
                </div>
                <RunEngineSelector
                  value={data.engine}
                  onChange={(value) => update({ engine: value })}
                  className="nopan w-52 text-xs"
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Max Steps Override
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["extraction"]["maxStepsOverride"]}
                  />
                </div>
                <Input
                  type="number"
                  placeholder={placeholders["extraction"]["maxStepsOverride"]}
                  className="nopan w-52 text-xs"
                  min="0"
                  value={data.maxStepsOverride ?? ""}
                  onChange={(event) => {
                    if (!editable) return;
                    const value =
                      event.target.value === ""
                        ? null
                        : Number(event.target.value);
                    update({ maxStepsOverride: value });
                  }}
                />
              </div>
              <BlockExecutionOptions
                continueOnFailure={data.continueOnFailure}
                nextLoopOnFailure={data.nextLoopOnFailure}
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="extraction"
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
                }
              />
              <DisableCache
                disableCache={data.disableCache}
                editable={editable}
                onDisableCacheChange={(disableCache) =>
                  update({ disableCache })
                }
              />
              <IgnoreWorkflowSystemPrompt
                ignoreWorkflowSystemPrompt={
                  data.ignoreWorkflowSystemPrompt ?? false
                }
                editable={editable}
                onIgnoreWorkflowSystemPromptChange={(
                  ignoreWorkflowSystemPrompt,
                ) => {
                  update({ ignoreWorkflowSystemPrompt });
                }}
              />
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { ExtractionEditor };
