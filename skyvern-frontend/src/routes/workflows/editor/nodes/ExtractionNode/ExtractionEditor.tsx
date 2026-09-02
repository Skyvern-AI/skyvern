import { RunEngine } from "@/api/types";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
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
import {
  extractionExportDataSchemaDefault,
  type ExtractionNode,
  type ExtractionNodeData,
} from "./types";
import { dataSchemaExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

const exportDataSchemaExampleValue = {
  type: "array",
  items: {
    type: "object",
    properties: { value: { type: "string" } },
  },
};

// One-time convenience: when the user first turns export on, seed its schema
// from the extraction schema they already wrote instead of leaving it blank.
// Only fires while exportDataSchema still holds its untouched default -- after
// that the two schemas are independent, so editing the extraction schema later
// never silently changes what gets exported.
function deriveExportDataSchema(dataSchema: string): string | null {
  try {
    const parsed = JSON.parse(dataSchema);
    if (parsed && typeof parsed === "object") {
      // Already array-of-objects shaped (the common "extract a list of rows"
      // case) -- pass it through as-is rather than wrapping it again.
      if (parsed.type === "array") {
        return JSON.stringify(parsed, null, 2);
      }
      if (parsed.type === "object") {
        return JSON.stringify({ type: "array", items: parsed }, null, 2);
      }
    }
  } catch {
    return null;
  }
  return null;
}

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
  const availableEngines = [
    RunEngine.SkyvernV1,
    RunEngine.SkyvernV3,
    RunEngine.OpenaiCua,
    RunEngine.AnthropicCua,
  ];
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
                    <Label className="text-xs text-tertiary-foreground">
                      Data Extraction Goal
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["extraction"]["dataExtractionGoal"]}
                    />
                  </div>
                  {isFirstWorkflowBlock ? (
                    <div className="flex justify-end text-xs text-muted-foreground">
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
        <AccordionItem value="export">
          <AccordionTrigger>Export</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-tertiary-foreground">
                    Export as Parquet
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["extraction"]["exportEnabled"]}
                  />
                </div>
                <div className="w-52">
                  <Switch
                    checked={data.exportEnabled}
                    data-testid="extraction-export-enabled-switch"
                    onCheckedChange={(checked) => {
                      if (!editable) return;
                      if (
                        checked &&
                        data.exportDataSchema ===
                          extractionExportDataSchemaDefault
                      ) {
                        const derived = deriveExportDataSchema(data.dataSchema);
                        if (derived) {
                          update({
                            exportEnabled: true,
                            exportDataSchema: derived,
                          });
                          return;
                        }
                      }
                      update({ exportEnabled: checked });
                    }}
                  />
                </div>
              </div>
              {data.exportEnabled ? (
                <>
                  <WorkflowDataSchemaInputGroup
                    value={data.exportDataSchema}
                    onChange={(value) => update({ exportDataSchema: value })}
                    exampleValue={exportDataSchemaExampleValue}
                    suggestionContext={{
                      current_schema: data.exportDataSchema,
                    }}
                  />
                  <div className="space-y-2">
                    <Label className="text-xs text-tertiary-foreground">
                      Filename (optional)
                    </Label>
                    <Input
                      className="nopan text-xs"
                      disabled={!editable}
                      onChange={(event) =>
                        update({ exportFileName: event.target.value })
                      }
                      placeholder="records"
                      value={data.exportFileName}
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-tertiary-foreground">
                        Records (optional)
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["extraction"]["exportRecords"]}
                      />
                    </div>
                    <Textarea
                      className="nopan min-h-16 font-mono text-xs"
                      disabled={!editable}
                      onChange={(event) =>
                        update({ exportRecords: event.target.value })
                      }
                      placeholder="Defaults to this block's own extracted output"
                      value={data.exportRecords}
                    />
                  </div>
                </>
              ) : null}
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
                  <Label className="text-xs font-normal text-tertiary-foreground">
                    Engine
                  </Label>
                </div>
                <RunEngineSelector
                  value={data.engine}
                  onChange={(value) => update({ engine: value })}
                  className="nopan w-52 text-xs"
                  availableEngines={availableEngines}
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-tertiary-foreground">
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
