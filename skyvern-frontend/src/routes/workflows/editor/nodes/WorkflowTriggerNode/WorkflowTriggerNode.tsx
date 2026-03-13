import { useCallback, useEffect, useRef, useState } from "react";
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
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { cn } from "@/util/utils";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { BrowserSessionSelector } from "./BrowserSessionSelector";
import {
  PARENT_SESSION_VALUE,
  FRESH_SESSION_VALUE,
} from "./browserSessionConstants";

const workflowPermanentIdTooltip =
  "Select the workflow to trigger when this block runs.";
const payloadTooltip =
  "Parameters to pass to the triggered workflow. Values support Jinja2 templates like {{ some_parameter }}.";
const waitForCompletionTooltip =
  "If enabled, this block will wait for the triggered workflow to complete before continuing to the next block. If disabled, the workflow is triggered asynchronously and the parent continues immediately. When disabled, the triggered workflow cannot continue in the same session because the parent may close it before the child finishes.";
const browserSessionTooltip =
  "Choose which browser session the triggered workflow should use. Continuing in the same session shares tabs, cookies, and login state. Creating a new browser gives the triggered workflow a fresh browser.";

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
  const { beginInternalUpdate, endInternalUpdate } =
    useWorkflowHasChangesStore();

  const {
    workflowParameters,
    isLoading: isLoadingParams,
    workflowTitle: fetchedTitle,
  } = useTargetWorkflowParametersQuery(data.workflowPermanentId);

  const isCustomBrowserSession =
    !data.useParentBrowserSession && data.browserSessionId !== "";

  const [useDynamicBrowserSession, setUseDynamicBrowserSession] = useState(
    isCustomBrowserSession,
  );

  const browserSessionValue = data.useParentBrowserSession
    ? PARENT_SESSION_VALUE
    : data.browserSessionId || FRESH_SESSION_VALUE;

  const handleBrowserSessionChange = useCallback(
    (value: string) => {
      if (value === PARENT_SESSION_VALUE) {
        update({ useParentBrowserSession: true, browserSessionId: "" });
      } else if (value === FRESH_SESSION_VALUE) {
        update({ useParentBrowserSession: false, browserSessionId: "" });
      } else {
        update({ useParentBrowserSession: false, browserSessionId: value });
      }
    },
    [update],
  );

  const handleWaitForCompletionChange = useCallback(
    (checked: boolean) => {
      if (!checked && data.useParentBrowserSession) {
        update({
          waitForCompletion: checked,
          useParentBrowserSession: false,
          browserSessionId: "",
        });
      } else {
        update({ waitForCompletion: checked });
      }
    },
    [update, data.useParentBrowserSession],
  );

  // Hydrate title from fetched workflow data (single API call, no duplicate)
  // Mark as internal update to prevent triggering "unsaved changes" dialog
  useEffect(() => {
    if (fetchedTitle && fetchedTitle !== data.workflowTitle) {
      beginInternalUpdate();
      update({ workflowTitle: fetchedTitle });
      let ended = false;
      const timer = setTimeout(() => {
        ended = true;
        endInternalUpdate();
      }, 50);
      return () => {
        clearTimeout(timer);
        if (!ended) {
          endInternalUpdate();
        }
      };
    }
  }, [
    fetchedTitle,
    data.workflowTitle,
    update,
    beginInternalUpdate,
    endInternalUpdate,
  ]);

  // Signal FlowRenderer to re-layout after async parameters load,
  // because the skeleton → actual fields transition changes node dimensions
  const prevIsLoadingRef = useRef(isLoadingParams);
  useEffect(() => {
    if (prevIsLoadingRef.current && !isLoadingParams) {
      window.dispatchEvent(new Event("workflow-trigger-content-changed"));
    }
    prevIsLoadingRef.current = isLoadingParams;
  }, [isLoadingParams]);

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
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Browser Session
                </Label>
                <HelpTooltip content={browserSessionTooltip} />
              </div>
              <button
                type="button"
                className="text-xs text-blue-400 hover:underline"
                onClick={() => {
                  const next = !useDynamicBrowserSession;
                  setUseDynamicBrowserSession(next);
                  if (!next) {
                    update({
                      useParentBrowserSession: data.waitForCompletion,
                      browserSessionId: "",
                    });
                  }
                }}
              >
                {useDynamicBrowserSession
                  ? "Use selector"
                  : "Use dynamic value"}
              </button>
            </div>
            {useDynamicBrowserSession ? (
              <WorkflowBlockInputTextarea
                nodeId={id}
                onChange={(value) => {
                  update({
                    useParentBrowserSession: false,
                    browserSessionId: value,
                  });
                }}
                value={data.browserSessionId}
                placeholder="e.g. {{ browser_session_id }}"
                className="nopan text-xs"
              />
            ) : (
              <BrowserSessionSelector
                value={browserSessionValue}
                onChange={handleBrowserSessionChange}
                waitForCompletion={data.waitForCompletion}
              />
            )}
          </div>
          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">
                Wait for Completion
              </Label>
              <HelpTooltip content={waitForCompletionTooltip} />
            </div>
            <div className="w-52">
              <Switch
                checked={data.waitForCompletion}
                onCheckedChange={handleWaitForCompletionChange}
              />
            </div>
          </div>
          {!data.waitForCompletion && !useDynamicBrowserSession && (
            <p className="text-xs text-slate-400">
              &quot;Continue in the same session&quot; is disabled because the
              parent workflow may close its browser before the triggered
              workflow finishes.
            </p>
          )}
          <Separator />
          <Accordion type="single" collapsible>
            <AccordionItem value="advanced" className="border-0">
              <AccordionTrigger className="py-0">
                Advanced Settings
              </AccordionTrigger>
              <AccordionContent className="space-y-4 pl-6 pr-1 pt-4">
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
                  hideTopSeparator
                />
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
