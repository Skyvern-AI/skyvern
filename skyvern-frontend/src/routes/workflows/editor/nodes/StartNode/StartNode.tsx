import { Handle, Node, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { StartNode } from "./types";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { useEffect, useState } from "react";
import { ProxyLocation } from "@/api/types";
import { Separator } from "@/components/ui/separator";
import {
  WorkflowModel,
  scriptableWorkflowBlockTypes,
  type WorkflowBlockType,
} from "@/routes/workflows/types/workflowTypes";
import { useToggleScriptForNodeCallback } from "@/routes/workflows/hooks/useToggleScriptForNodeCallback";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import { Flippable } from "@/components/Flippable";
import { useRerender } from "@/hooks/useRerender";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { cn } from "@/util/utils";
import { BuildModeOnly } from "../BuildModeOnly";
import { isLoopNode } from "../LoopNode/types";
import { WorkflowSettingsEditor } from "./WorkflowSettingsEditor";

interface StartSettings {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  persistBrowserSession: boolean;
  browserProfileId: string | null;
  model: WorkflowModel | null;
  maxScreenshotScrollingTimes: number | null;
  extraHttpHeaders: string | Record<string, unknown> | null;
  finallyBlockLabel: string | null;
  workflowSystemPrompt: string | null;
}

function StartNode({ id, data, parentId }: NodeProps<StartNode>) {
  const workflowSettingsStore = useWorkflowSettingsStore();
  const reactFlowInstance = useReactFlow();
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const recordingStore = useRecordingStore();
  const script = blockScriptStore.scripts.__start_block__;
  const rerender = useRerender({ prefix: "accordion" });
  const toggleScriptForNodeCallback = useToggleScriptForNodeCallback();
  const isRecording = recordingStore.isRecording;

  const parentNode = parentId ? reactFlowInstance.getNode(parentId) : null;
  const isInsideConditional = parentNode?.type === "conditional";
  const loopParent = parentNode && isLoopNode(parentNode) ? parentNode : null;

  const makeStartSettings = (data: StartNode["data"]): StartSettings => {
    return {
      webhookCallbackUrl: data.withWorkflowSettings
        ? data.webhookCallbackUrl
        : "",
      proxyLocation: data.withWorkflowSettings
        ? data.proxyLocation
        : ProxyLocation.Residential,
      persistBrowserSession: data.withWorkflowSettings
        ? data.persistBrowserSession
        : false,
      browserProfileId: data.withWorkflowSettings
        ? data.browserProfileId
        : null,
      model: data.withWorkflowSettings ? data.model : null,
      maxScreenshotScrollingTimes: data.withWorkflowSettings
        ? data.maxScreenshotScrolls
        : null,
      extraHttpHeaders: data.withWorkflowSettings
        ? data.extraHttpHeaders
        : null,
      finallyBlockLabel: data.withWorkflowSettings
        ? data.finallyBlockLabel
        : null,
      workflowSystemPrompt: data.withWorkflowSettings
        ? (data.workflowSystemPrompt ?? null)
        : null,
    };
  };

  useEffect(() => {
    setFacing(data.showCode ? "back" : "front");
  }, [data.showCode]);

  useEffect(() => {
    workflowSettingsStore.setWorkflowSettings(makeStartSettings(data));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  function nodeIsFlippable(node: Node) {
    return (
      scriptableWorkflowBlockTypes.has(node.type as WorkflowBlockType) ||
      node.type === "start"
    );
  }

  // NOTE(jdo): keeping for reference; we seem to revert stuff all the time
  // function showAllScripts() {
  //   for (const node of reactFlowInstance.getNodes()) {
  //     const label = node.data.label;

  //     label &&
  //       nodeIsFlippable(node) &&
  //       typeof label === "string" &&
  //       toggleScriptForNodeCallback({
  //         label,
  //         show: true,
  //       });
  //   }
  // }

  function hideAllScripts() {
    for (const node of reactFlowInstance.getNodes()) {
      const label = node.data.label;

      label &&
        nodeIsFlippable(node) &&
        typeof label === "string" &&
        toggleScriptForNodeCallback({
          label,
          show: false,
        });
    }
  }

  if (data.withWorkflowSettings) {
    return (
      <Flippable facing={facing} preserveFrontsideHeight={true}>
        <div>
          <Handle
            type="source"
            position={Position.Bottom}
            id="a"
            className="opacity-0"
          />
          <div
            className={cn(
              "w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 text-center",
              { "h-[20rem] overflow-hidden": facing === "back" },
            )}
          >
            <div className="relative">
              <header className="mb-6 mt-2">Start</header>
              <Separator />
              <BuildModeOnly renderInReadOnlyComparison={false}>
                <Accordion
                  type="single"
                  collapsible
                  onValueChange={() => rerender.bump()}
                >
                  <AccordionItem value="settings" className="mt-4 border-b-0">
                    <AccordionTrigger className="py-2">
                      Workflow Settings
                    </AccordionTrigger>
                    <AccordionContent className="pl-6 pr-1 pt-1">
                      <div key={rerender.key}>
                        <WorkflowSettingsEditor blockId={id} />
                      </div>
                    </AccordionContent>
                  </AccordionItem>
                </Accordion>
              </BuildModeOnly>
            </div>
          </div>
        </div>

        <BlockCodeEditor
          blockLabel="__start_block__"
          title="Start"
          script={script}
          onExit={() => {
            hideAllScripts();
            return false;
          }}
        />
      </Flippable>
    );
  }

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": isRecording,
      })}
    >
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <div className="w-[30rem] rounded-lg bg-slate-elevation4 px-6 py-4 text-center text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">
        Start
        {loopParent ? (
          <div className="workflow-editor-tip mt-4 flex gap-3 rounded-md bg-slate-800 p-3 normal-case tracking-normal">
            <span className="workflow-editor-tip-icon rounded bg-slate-700 p-1 text-lg">
              💡
            </span>
            <div className="space-y-1 text-left font-normal text-slate-400">
              {loopParent.data.loopKind === "while" ? (
                <>
                  Use{" "}
                  <code className="text-white">{`{{ current_index }}`}</code> to
                  get the current zero-based loop index for a given iteration.
                </>
              ) : (
                <>
                  Use{" "}
                  <code className="text-white">{`{{ current_value }}`}</code> to
                  get the current loop value for a given iteration.
                </>
              )}
            </div>
          </div>
        ) : null}
        {isInsideConditional && (
          <div className="mt-4 rounded-md border border-dashed border-slate-500 p-4 text-center font-normal normal-case tracking-normal text-slate-300">
            Start adding blocks to be executed for the selected condition
          </div>
        )}
      </div>
    </div>
  );
}

export { StartNode };
