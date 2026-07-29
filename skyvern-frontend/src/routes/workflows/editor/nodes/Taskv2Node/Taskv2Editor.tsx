import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import {
  MAX_STEPS_DEFAULT,
  type Taskv2Node,
  type Taskv2NodeData,
} from "./types";
import { useUpdate } from "../../useUpdate";

function Taskv2Editor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<Taskv2Node>(blockId);
  if (!nodeSlice || nodeSlice.type !== "taskv2") {
    return null;
  }
  return <Taskv2EditorBody blockId={blockId} data={nodeSlice.data} />;
}

function Taskv2EditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: Taskv2NodeData;
}) {
  const { editable } = data;
  const update = useUpdate<Taskv2NodeData>({ id: blockId, editable });

  return (
    <div
      data-testid="taskv2-block-form"
      data-block-id={blockId}
      className="space-y-4"
    >
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">URL</Label>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(value) => update({ url: value })}
          value={data.url}
          placeholder={placeholders["taskv2"]["url"]}
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">Prompt</Label>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.taskV2.prompt}
          nodeId={blockId}
          onChange={(value) => update({ prompt: value })}
          value={data.prompt}
          placeholder={placeholders["taskv2"]["prompt"]}
          className="nopan text-xs"
        />
      </div>
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <div className="space-y-4">
              <ModelSelector
                className="nopan w-52 text-xs"
                value={data.model}
                onChange={(value) => update({ model: value })}
              />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Max Steps
                  </Label>
                  <HelpTooltip content={helpTooltips["taskv2"]["maxSteps"]} />
                </div>
                <Input
                  type="number"
                  placeholder="10"
                  className="nopan text-xs"
                  value={data.maxSteps ?? MAX_STEPS_DEFAULT}
                  onChange={(event) =>
                    update({ maxSteps: Number(event.target.value) })
                  }
                />
              </div>
              <Separator />
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
              <Separator />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    2FA Identifier
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["taskv2"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ totpIdentifier: value })}
                  value={data.totpIdentifier ?? ""}
                  placeholder={placeholders["navigation"]["totpIdentifier"]}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    2FA Verification URL
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["task"]["totpVerificationUrl"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ totpVerificationUrl: value })}
                  value={data.totpVerificationUrl ?? ""}
                  placeholder={placeholders["task"]["totpVerificationUrl"]}
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { Taskv2Editor };
