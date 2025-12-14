import { Handle, Node, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { StartNode } from "./types";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useEffect, useState } from "react";
import { ProxyLocation } from "@/api/types";
import { Label } from "@/components/ui/label";
import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowModel } from "@/routes/workflows/types/workflowTypes";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "../Taskv2Node/types";
import { KeyValueInput } from "@/components/KeyValueInput";
import { placeholders } from "@/routes/workflows/editor/helpContent";
import { useToggleScriptForNodeCallback } from "@/routes/workflows/hooks/useToggleScriptForNodeCallback";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import {
  scriptableWorkflowBlockTypes,
  type WorkflowBlockType,
} from "@/routes/workflows/types/workflowTypes";
import { Flippable } from "@/components/Flippable";
import { useRerender } from "@/hooks/useRerender";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";

interface StartSettings {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  persistBrowserSession: boolean;
  model: WorkflowModel | null;
  maxScreenshotScrollingTimes: number | null;
  extraHttpHeaders: string | Record<string, unknown> | null;
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
  const isInsideLoop = parentNode?.type === "loop";

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
      model: data.withWorkflowSettings ? data.model : null,
      maxScreenshotScrollingTimes: data.withWorkflowSettings
        ? data.maxScreenshotScrolls
        : null,
      extraHttpHeaders: data.withWorkflowSettings
        ? data.extraHttpHeaders
        : null,
    };
  };

  const update = useUpdate<StartNode["data"]>({ id, editable: true });

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
                    <div key={rerender.key} className="space-y-4">
                      <div className="space-y-2">
                        <ModelSelector
                          className="nopan w-52 text-xs"
                          value={data.model}
                          onChange={(value) => {
                            update({ model: value });
                          }}
                        />
                      </div>
                      <div className="space-y-2">
                        <div className="flex gap-2">
                          <Label>Webhook Callback URL</Label>
                          <HelpTooltip content="The URL of a webhook endpoint to send the workflow results" />
                        </div>
                        <div className="flex flex-col gap-2">
                          <Input
                            className="w-full"
                            value={data.webhookCallbackUrl}
                            placeholder="https://"
                            onChange={(event) => {
                              update({
                                webhookCallbackUrl: event.target.value,
                              });
                            }}
                          />
                          <TestWebhookDialog
                            runType="workflow_run"
                            runId={null}
                            initialWebhookUrl={
                              data.webhookCallbackUrl || undefined
                            }
                            trigger={
                              <Button
                                type="button"
                                variant="secondary"
                                className="self-start"
                                disabled={!data.webhookCallbackUrl}
                              >
                                Test Webhook
                              </Button>
                            }
                          />
                        </div>
                      </div>
                      <div className="space-y-2">
                        <div className="flex gap-2">
                          <Label>Proxy Location</Label>
                          <HelpTooltip content="Route Skyvern through one of our available proxies." />
                        </div>
                        <ProxySelector
                          value={data.proxyLocation}
                          onChange={(value) => {
                            update({ proxyLocation: value });
                          }}
                        />
                      </div>
                      <div className="flex flex-col gap-4 rounded-md bg-slate-elevation5 p-4 pl-4">
                        <div className="flex flex-col gap-4">
                          <div className="flex justify-between">
                            <div className="flex items-center gap-2">
                              <Label>Run With</Label>
                              <HelpTooltip content="If code has been generated and saved from a previously successful run, set this to 'Code' to use that code when executing the workflow. To avoid using code, set this to 'Skyvern Agent'." />
                            </div>
                            <Select
                              value={data.runWith ?? "agent"}
                              onValueChange={(value) => {
                                update({ runWith: value });
                              }}
                            >
                              <SelectTrigger className="w-48">
                                <SelectValue placeholder="Run Method" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="agent">
                                  Skyvern Agent
                                </SelectItem>
                                <SelectItem value="code">Code</SelectItem>
                              </SelectContent>
                            </Select>
                          </div>
                          <div className="space-y-2">
                            <div className="flex items-center gap-2">
                              <Label>AI Fallback (self-healing)</Label>
                              <HelpTooltip content="If a run with code fails, fallback to AI and regenerate the code." />
                              <Switch
                                className="ml-auto"
                                checked={data.aiFallback}
                                onCheckedChange={(value) => {
                                  update({ aiFallback: value });
                                }}
                              />
                            </div>
                          </div>
                          <div className="space-y-2">
                            <div className="flex gap-2">
                              <Label>Code Key (optional)</Label>
                              <HelpTooltip content="A static or dynamic key for directing code generation." />
                            </div>
                            <WorkflowBlockInputTextarea
                              nodeId={id}
                              onChange={(value) => {
                                const v = value.length ? value : null;
                                update({ scriptCacheKey: v });
                              }}
                              value={data.scriptCacheKey ?? ""}
                              placeholder={placeholders["scripts"]["scriptKey"]}
                              className="nopan text-xs"
                            />
                          </div>
                        </div>
                        {/* )} */}
                      </div>
                      <div className="flex flex-col gap-4">
                        <div className="space-y-2">
                          <div className="flex items-center gap-2">
                            <Label>Run Sequentially</Label>
                            <HelpTooltip content="Run the workflow in a sequential order" />
                            <Switch
                              className="ml-auto"
                              checked={data.runSequentially}
                              onCheckedChange={(value) => {
                                update({
                                  runSequentially: value,
                                  sequentialKey: value
                                    ? data.sequentialKey
                                    : null,
                                });
                              }}
                            />
                          </div>
                        </div>
                        {data.runSequentially && (
                          <div className="flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4 pl-4">
                            <div className="space-y-2">
                              <div className="flex gap-2">
                                <Label>Sequential Key (optional)</Label>
                                <HelpTooltip content="A static or dynamic key for directing sequential workflow execution." />
                              </div>
                              <WorkflowBlockInputTextarea
                                nodeId={id}
                                onChange={(value) => {
                                  const v = value.length ? value : null;
                                  update({ sequentialKey: v });
                                }}
                                value={data.sequentialKey ?? ""}
                                placeholder={placeholders["sequentialKey"]}
                                className="nopan text-xs"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Label>Save &amp; Reuse Session</Label>
                          <HelpTooltip content="Persist session information across workflow runs" />
                          <Switch
                            className="ml-auto"
                            checked={data.persistBrowserSession}
                            onCheckedChange={(value) => {
                              update({ persistBrowserSession: value });
                            }}
                          />
                        </div>
                      </div>
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Label>Extra HTTP Headers</Label>
                          <HelpTooltip content="Specify some self-defined HTTP requests headers" />
                        </div>
                        <KeyValueInput
                          value={
                            data.extraHttpHeaders &&
                            typeof data.extraHttpHeaders === "object"
                              ? JSON.stringify(data.extraHttpHeaders)
                              : data.extraHttpHeaders ?? null
                          }
                          onChange={(val) => {
                            const v =
                              val === null
                                ? "{}"
                                : typeof val === "string"
                                  ? val.trim()
                                  : JSON.stringify(val);

                            const normalized = v === "" ? "{}" : v;

                            if (normalized === data.extraHttpHeaders) {
                              return;
                            }

                            update({ extraHttpHeaders: normalized });
                          }}
                          addButtonText="Add Header"
                        />
                      </div>
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Label>Max Screenshot Scrolls</Label>
                          <HelpTooltip
                            content={`The maximum number of scrolls for the post action screenshot. Default is ${MAX_SCREENSHOT_SCROLLS_DEFAULT}. If it's set to 0, it will take the current viewport screenshot.`}
                          />
                        </div>
                        <Input
                          value={data.maxScreenshotScrolls ?? ""}
                          placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
                          onChange={(event) => {
                            const value =
                              event.target.value === ""
                                ? null
                                : Number(event.target.value);

                            update({ maxScreenshotScrolls: value });
                          }}
                        />
                      </div>
                    </div>
                  </AccordionContent>
                </AccordionItem>
              </Accordion>
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
        {isInsideLoop && (
          <div className="mt-4 flex gap-3 rounded-md bg-slate-800 p-3 normal-case tracking-normal">
            <span className="rounded bg-slate-700 p-1 text-lg">💡</span>
            <div className="space-y-1 text-left font-normal text-slate-400">
              Use{" "}
              <code className="text-white">
                &#123;&#123;&nbsp;current_value&nbsp;&#125;&#125;
              </code>{" "}
              to get the current loop value for a given iteration.
            </div>
          </div>
        )}
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
