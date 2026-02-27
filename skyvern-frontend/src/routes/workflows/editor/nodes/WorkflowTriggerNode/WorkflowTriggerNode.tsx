import { useCallback, useEffect } from "react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { NodeHeader } from "../components/NodeHeader";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import type { WorkflowTriggerNode as WorkflowTriggerNodeType } from "./types";
import { isConcreteWpid } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowSelector } from "./WorkflowSelector";
import { PayloadParameterFields } from "./PayloadParameterFields";
import { useTargetWorkflowParametersQuery } from "./useTargetWorkflowParametersQuery";
import { AppNode } from "..";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";

const workflowPermanentIdTooltip =
  "Select the workflow to trigger when this block runs.";
const payloadTooltip =
  "Parameters to pass to the triggered workflow. Values support Jinja2 templates like {{ some_parameter }}.";
const waitForCompletionTooltip =
  "If enabled, this block will wait for the triggered workflow to complete before continuing to the next block. If disabled, the workflow is triggered asynchronously and execution continues immediately.";
const useParentBrowserSessionTooltip =
  "When enabled, the triggered workflow will use the same browser session as the parent workflow, continuing where it left off (same tabs, cookies, login state).";
const browserSessionIdTooltip =
  "Optional browser session ID to pass to the triggered workflow. This allows the triggered workflow to reuse an existing browser session. Overrides the parent session toggle if set.";

function WorkflowTriggerNode({ id, data }: NodeProps<WorkflowTriggerNodeType>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel, workflowPermanentId } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const nodes = useNodes<AppNode>();
  const isInsideForLoop = isNodeInsideForLoop(nodes, id);
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    id,
  );

  const update = useUpdate<WorkflowTriggerNodeType["data"]>({ id, editable });
  const recordingStore = useRecordingStore();

  const {
    workflowParameters,
    isLoading: isLoadingParams,
    workflowTitle: fetchedTitle,
  } = useTargetWorkflowParametersQuery(data.workflowPermanentId);

  // Hydrate title from fetched workflow data (single API call, no duplicate)
  useEffect(() => {
    if (fetchedTitle && fetchedTitle !== data.workflowTitle) {
      update({ workflowTitle: fetchedTitle });
    }
  }, [fetchedTitle, data.workflowTitle, update]);

  const hasWorkflowSelected = isConcreteWpid(data.workflowPermanentId);

  const handleTitleChange = useCallback(
    (title: string) => {
      update({ workflowTitle: title });
    },
    [update],
  );

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
          totpIdentifier={null}
          totpUrl={null}
          type={WorkflowBlockTypes.WorkflowTrigger}
        />
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Target Workflow</Label>
              <HelpTooltip content={workflowPermanentIdTooltip} />
            </div>
            <WorkflowSelector
              nodeId={id}
              value={data.workflowPermanentId}
              onChange={(value) => {
                if (value === data.workflowPermanentId) return;
                update({ workflowPermanentId: value, payload: "{}" });
              }}
              workflowTitle={data.workflowTitle}
              onTitleChange={handleTitleChange}
              excludeWorkflowPermanentId={workflowPermanentId}
            />
          </div>
          <Separator />
          <div className="space-y-4">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Payload</Label>
              <HelpTooltip content={payloadTooltip} />
            </div>
            {hasWorkflowSelected ? (
              <PayloadParameterFields
                parameters={workflowParameters}
                payload={data.payload}
                onChange={(value) => {
                  update({ payload: value });
                }}
                nodeId={id}
                isLoading={isLoadingParams}
              />
            ) : (
              <p className="text-xs text-slate-500">
                Select a target workflow to configure its input parameters here.
              </p>
            )}
          </div>
          <Separator />
          <Accordion type="single" collapsible>
            <AccordionItem value="advanced" className="border-b-0">
              <AccordionTrigger className="py-0">
                Advanced Settings
              </AccordionTrigger>
              <AccordionContent className="space-y-4 pl-6 pr-1 pt-4">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Wait for Completion
                    </Label>
                    <HelpTooltip content={waitForCompletionTooltip} />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={data.waitForCompletion}
                      onCheckedChange={(checked) => {
                        update({ waitForCompletion: checked });
                      }}
                    />
                  </div>
                </div>
                <BlockExecutionOptions
                  continueOnFailure={data.continueOnFailure}
                  nextLoopOnFailure={data.nextLoopOnFailure}
                  editable={editable}
                  isInsideForLoop={isInsideForLoop}
                  blockType="workflowTrigger"
                  onContinueOnFailureChange={(checked) => {
                    update({ continueOnFailure: checked });
                  }}
                  onNextLoopOnFailureChange={(checked) => {
                    update({ nextLoopOnFailure: checked });
                  }}
                />
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Use Parent Browser Session
                      </Label>
                      <HelpTooltip content={useParentBrowserSessionTooltip} />
                    </div>
                    <div className="w-52">
                      <Switch
                        checked={data.useParentBrowserSession}
                        onCheckedChange={(checked) => {
                          update({ useParentBrowserSession: checked });
                        }}
                      />
                    </div>
                  </div>
                  {!data.waitForCompletion && data.useParentBrowserSession && (
                    <p className="text-xs text-yellow-500">
                      Using the parent browser session while "Wait for
                      Completion" is off may cause the triggered workflow to
                      fail if the parent finishes and closes the browser first.
                    </p>
                  )}
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Browser Session ID
                    </Label>
                    <HelpTooltip content={browserSessionIdTooltip} />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      update({ browserSessionId: value });
                    }}
                    value={data.browserSessionId}
                    placeholder="Optional: {{ browser_session_id }}"
                    className="nopan text-xs"
                  />
                </div>
                <Separator />
                <ParametersMultiSelect
                  availableOutputParameters={availableOutputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) => {
                    update({ parameterKeys });
                  }}
                />
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
    </div>
  );
}

export { WorkflowTriggerNode };
