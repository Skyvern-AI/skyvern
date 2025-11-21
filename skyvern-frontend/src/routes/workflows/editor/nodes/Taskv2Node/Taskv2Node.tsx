import { useEffect, useState } from "react";
import { Flippable } from "@/components/Flippable";
import { HelpTooltip } from "@/components/HelpTooltip";
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
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { MAX_STEPS_DEFAULT, type Taskv2Node } from "./types";
import { ModelSelector } from "@/components/ModelSelector";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { NodeTabs } from "../components/NodeTabs";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRerender } from "@/hooks/useRerender";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useBlockScriptStore } from "@/store/BlockScriptStore";

import { DisableCache } from "../DisableCache";

function Taskv2Node({ id, data, type }: NodeProps<Taskv2Node>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const script = blockScriptStore.scripts[label];
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const rerender = useRerender({ prefix: "accordian" });
  const update = useUpdate<Taskv2Node["data"]>({ id, editable });

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
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={data.totpIdentifier}
            totpUrl={data.totpVerificationUrl}
            type="task_v2" // sic: the naming is not consistent
          />
          <div className="space-y-4">
            <div className="space-y-2">
              <Label className="text-xs text-slate-300">URL</Label>
              <WorkflowBlockInputTextarea
                canWriteTitle={true}
                nodeId={id}
                onChange={(value) => {
                  update({ url: value });
                }}
                value={data.url}
                placeholder={placeholders[type]["url"]}
                className="nopan text-xs"
              />
            </div>
            <div className="space-y-2">
              <div className="flex justify-between">
                <Label className="text-xs text-slate-300">Prompt</Label>
                {isFirstWorkflowBlock ? (
                  <div className="flex justify-end text-xs text-slate-400">
                    Tip: Use the {"+"} button to add parameters!
                  </div>
                ) : null}
              </div>
              <WorkflowBlockInputTextarea
                aiImprove={AI_IMPROVE_CONFIGS.taskV2.prompt}
                nodeId={id}
                onChange={(value) => {
                  update({ prompt: value });
                }}
                value={data.prompt}
                placeholder={placeholders[type]["prompt"]}
                className="nopan text-xs"
              />
            </div>
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
              <AccordionContent key={rerender.key} className="pl-6 pr-1 pt-4">
                <div className="space-y-4">
                  <ModelSelector
                    className="nopan w-52 text-xs"
                    value={data.model}
                    onChange={(value) => {
                      update({ model: value });
                    }}
                  />
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Max Steps
                      </Label>
                      <HelpTooltip content={helpTooltips[type]["maxSteps"]} />
                    </div>
                    <Input
                      type="number"
                      placeholder="10"
                      className="nopan text-xs"
                      value={data.maxSteps ?? MAX_STEPS_DEFAULT}
                      onChange={(event) => {
                        update({
                          maxSteps: Number(event.target.value),
                        });
                      }}
                    />
                  </div>
                  <Separator />
                  <DisableCache
                    disableCache={data.disableCache}
                    editable={editable}
                    onDisableCacheChange={(disableCache) => {
                      update({ disableCache });
                    }}
                  />
                  <Separator />
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        2FA Identifier
                      </Label>
                      <HelpTooltip
                        content={helpTooltips[type]["totpIdentifier"]}
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ totpIdentifier: value });
                      }}
                      value={data.totpIdentifier ?? ""}
                      placeholder={placeholders["navigation"]["totpIdentifier"]}
                      className="nopan text-xs"
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        2FA Verification URL
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["task"]["totpVerificationUrl"]}
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ totpVerificationUrl: value });
                      }}
                      value={data.totpVerificationUrl ?? ""}
                      placeholder={placeholders["task"]["totpVerificationUrl"]}
                      className="nopan text-xs"
                    />
                  </div>
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
          <NodeTabs blockLabel={label} />
        </div>
      </div>

      <BlockCodeEditor blockLabel={label} blockType="task_v2" script={script} />
    </Flippable>
  );
}

export { Taskv2Node };
