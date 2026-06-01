import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

import { type AppNode } from "..";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { BrowserSessionSelector } from "./BrowserSessionSelector";
import {
  FRESH_SESSION_VALUE,
  PARENT_SESSION_VALUE,
} from "./browserSessionConstants";
import { PayloadParameterFields } from "./PayloadParameterFields";
import {
  isConcreteWpid,
  type WorkflowTriggerNode,
  type WorkflowTriggerNodeData,
} from "./types";
import { useTargetWorkflowParametersQuery } from "./useTargetWorkflowParametersQuery";
import { WorkflowSelector } from "./WorkflowSelector";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

const workflowPermanentIdTooltip =
  "Select the agent to trigger when this block runs.";
const payloadTooltip =
  "Inputs to pass to the triggered agent. Values support Jinja2 templates like {{ some_parameter }}.";
const waitForCompletionTooltip =
  "If enabled, this block will wait for the triggered agent to complete before continuing to the next block. If disabled, the agent is triggered asynchronously and the parent continues immediately. When disabled, the triggered agent cannot continue in the same session because the parent may close it before the child finishes.";
const browserSessionTooltip =
  "Choose which browser session the triggered agent should use. Continuing in the same session shares tabs, cookies, and login state. Creating a new browser gives the triggered agent a fresh browser.";

function WorkflowTriggerEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside
  // the per-node renderer and the body also subscribes to useNodes()/
  // useEdges() for parent-loop checks; a one-time getNode() snapshot would
  // re-render with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<WorkflowTriggerNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "workflowTrigger") {
    return null;
  }
  return <WorkflowTriggerEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function WorkflowTriggerEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: WorkflowTriggerNodeData;
}) {
  const {
    editable,
    workflowPermanentId,
    workflowTitle,
    payload,
    waitForCompletion,
    browserSessionId,
    useParentBrowserSession,
    parameterKeys,
    continueOnFailure,
    nextLoopOnFailure,
  } = data;

  const { workflowPermanentId: parentWorkflowPermanentId } = useParams();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  const update = useUpdate<WorkflowTriggerNodeData>({ id: blockId, editable });
  const { beginInternalUpdate, endInternalUpdate } =
    useWorkflowHasChangesStore();

  const {
    workflowParameters,
    isLoading: isLoadingParams,
    workflowTitle: fetchedTitle,
  } = useTargetWorkflowParametersQuery(workflowPermanentId);

  const isCustomBrowserSession =
    !useParentBrowserSession && browserSessionId !== "";

  const [useDynamicBrowserSession, setUseDynamicBrowserSession] = useState(
    isCustomBrowserSession,
  );

  const browserSessionValue = useParentBrowserSession
    ? PARENT_SESSION_VALUE
    : browserSessionId || FRESH_SESSION_VALUE;

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
      if (!checked && useParentBrowserSession) {
        update({
          waitForCompletion: checked,
          useParentBrowserSession: false,
          browserSessionId: "",
        });
      } else {
        update({ waitForCompletion: checked });
      }
    },
    [update, useParentBrowserSession],
  );

  const handleTitleChange = useCallback(
    (title: string) => {
      update({ workflowTitle: title });
    },
    [update],
  );

  useEffect(() => {
    if (fetchedTitle && fetchedTitle !== workflowTitle) {
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
    workflowTitle,
    update,
    beginInternalUpdate,
    endInternalUpdate,
  ]);

  const hasWorkflowSelected = isConcreteWpid(workflowPermanentId);

  return (
    <div data-testid="workflow-trigger-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Target Agent</Label>
          <HelpTooltip content={workflowPermanentIdTooltip} />
        </div>
        <WorkflowSelector
          nodeId={blockId}
          value={workflowPermanentId}
          onChange={(next) => {
            if (next === workflowPermanentId) return;
            update({ workflowPermanentId: next, payload: "{}" });
          }}
          workflowTitle={workflowTitle}
          onTitleChange={handleTitleChange}
          excludeWorkflowPermanentId={parentWorkflowPermanentId}
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
            payload={payload}
            onChange={(next) => update({ payload: next })}
            nodeId={blockId}
            isLoading={isLoadingParams}
          />
        ) : (
          <p className="text-xs text-slate-500">
            Select a target agent to configure its inputs here.
          </p>
        )}
      </div>
      <Separator />
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Browser Session</Label>
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
                  useParentBrowserSession: waitForCompletion,
                  browserSessionId: "",
                });
              }
            }}
          >
            {useDynamicBrowserSession ? "Use selector" : "Use dynamic value"}
          </button>
        </div>
        {useDynamicBrowserSession ? (
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(next) => {
              update({
                useParentBrowserSession: false,
                browserSessionId: next,
              });
            }}
            value={browserSessionId}
            placeholder="e.g. {{ browser_session_id }}"
            className="nopan text-xs"
          />
        ) : (
          <BrowserSessionSelector
            value={browserSessionValue}
            onChange={handleBrowserSessionChange}
            waitForCompletion={waitForCompletion}
          />
        )}
      </div>
      <div className="flex items-center justify-between">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Wait for Completion</Label>
          <HelpTooltip content={waitForCompletionTooltip} />
        </div>
        <div className="w-52">
          <Switch
            checked={waitForCompletion}
            onCheckedChange={handleWaitForCompletionChange}
          />
        </div>
      </div>
      {!waitForCompletion && !useDynamicBrowserSession && (
        <p className="text-xs text-slate-400">
          &quot;Continue in the same session&quot; is disabled because the
          parent agent may close its browser before the triggered agent
          finishes.
        </p>
      )}
      <Separator />
      <Accordion type="single" collapsible defaultValue="advanced">
        <AccordionItem value="advanced" className="border-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="space-y-4 pl-6 pr-1 pt-4">
            <BlockExecutionOptions
              continueOnFailure={continueOnFailure}
              nextLoopOnFailure={nextLoopOnFailure}
              editable={editable}
              isInsideForLoop={isInsideForLoop}
              parentLoopSkipsOnFail={parentLoopSkipsOnFail}
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
              parameters={parameterKeys}
              onParametersChange={(next) => {
                update({ parameterKeys: next });
              }}
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
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { WorkflowTriggerEditor };
