import { getClient } from "@/api/AxiosClient";
import { Handle, Node, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { StartNode } from "./types";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { useEffect, useState } from "react";
import { ProxyLocation } from "@/api/types";
import { useQuery } from "@tanstack/react-query";
import { Label } from "@/components/ui/label";
import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { ModelsResponse } from "@/api/types";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowModel } from "@/routes/workflows/types/workflowTypes";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "../Taskv2Node/types";
import { KeyValueInput } from "@/components/KeyValueInput";
import { OrgWalled } from "@/components/Orgwalled";
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
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { cn } from "@/util/utils";
import { LightningBoltIcon } from "@radix-ui/react-icons";

function StartNode({ id, data }: NodeProps<StartNode>) {
  const workflowSettingsStore = useWorkflowSettingsStore();
  const credentialGetter = useCredentialGetter();
  const { updateNodeData } = useReactFlow();
  const reactFlowInstance = useReactFlow();

  const { data: availableModels } = useQuery<ModelsResponse>({
    queryKey: ["models"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);

      return client.get("/models").then((res) => res.data);
    },
  });

  const modelNames = availableModels?.models ?? {};
  const firstKey = Object.keys(modelNames)[0];
  const workflowModel: WorkflowModel | null = firstKey
    ? { model_name: modelNames[firstKey] || "" }
    : null;

  const [inputs, setInputs] = useState({
    webhookCallbackUrl: data.withWorkflowSettings
      ? data.webhookCallbackUrl
      : "",
    proxyLocation: data.withWorkflowSettings
      ? data.proxyLocation
      : ProxyLocation.Residential,
    persistBrowserSession: data.withWorkflowSettings
      ? data.persistBrowserSession
      : false,
    model: data.withWorkflowSettings ? data.model : workflowModel,
    maxScreenshotScrolls: data.withWorkflowSettings
      ? data.maxScreenshotScrolls
      : null,
    extraHttpHeaders: data.withWorkflowSettings ? data.extraHttpHeaders : null,
    useScriptCache: data.withWorkflowSettings ? data.useScriptCache : false,
    scriptCacheKey: data.withWorkflowSettings ? data.scriptCacheKey : null,
    aiFallback: data.withWorkflowSettings ? data.aiFallback : false,
  });

  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const script = blockScriptStore.scripts.__start_block__;
  const rerender = useRerender({ prefix: "accordion" });
  const toggleScriptForNodeCallback = useToggleScriptForNodeCallback();

  useEffect(() => {
    setFacing(data.showCode ? "back" : "front");
  }, [data.showCode]);

  useEffect(() => {
    workflowSettingsStore.setWorkflowSettings(inputs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputs]);

  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  function nodeIsFlippable(node: Node) {
    return (
      scriptableWorkflowBlockTypes.has(node.type as WorkflowBlockType) ||
      node.type === "start"
    );
  }

  function showAllScripts() {
    for (const node of reactFlowInstance.getNodes()) {
      const label = node.data.label;

      label &&
        nodeIsFlippable(node) &&
        typeof label === "string" &&
        toggleScriptForNodeCallback({
          label,
          show: true,
        });
    }
  }

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
              <div className="absolute right-[-0.5rem] top-[-0.25rem]">
                <div>
                  <OrgWalled className="p-0">
                    <Button variant="link" size="icon" onClick={showAllScripts}>
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <LightningBoltIcon className="h-4 w-4 text-[gold]" />
                          </TooltipTrigger>
                          <TooltipContent>Show all code</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </Button>
                  </OrgWalled>
                </div>
              </div>
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
                          value={inputs.model}
                          onChange={(value) => {
                            handleChange("model", value);
                          }}
                        />
                      </div>
                      <div className="space-y-2">
                        <div className="flex gap-2">
                          <Label>Webhook Callback URL</Label>
                          <HelpTooltip content="The URL of a webhook endpoint to send the workflow results" />
                        </div>
                        <Input
                          value={inputs.webhookCallbackUrl}
                          placeholder="https://"
                          onChange={(event) => {
                            handleChange(
                              "webhookCallbackUrl",
                              event.target.value,
                            );
                          }}
                        />
                      </div>
                      <div className="space-y-2">
                        <div className="flex gap-2">
                          <Label>Proxy Location</Label>
                          <HelpTooltip content="Route Skyvern through one of our available proxies." />
                        </div>
                        <ProxySelector
                          value={inputs.proxyLocation}
                          onChange={(value) => {
                            handleChange("proxyLocation", value);
                          }}
                        />
                      </div>
                      <OrgWalled className="p-0 hover:p-0">
                        <div className="flex flex-col gap-4">
                          <div className="space-y-2">
                            <div className="flex items-center gap-2">
                              <Label>Generate Code</Label>
                              <HelpTooltip content="Generate & use cached code for faster execution." />
                              <Switch
                                className="ml-auto"
                                checked={inputs.useScriptCache}
                                onCheckedChange={(value) => {
                                  handleChange("useScriptCache", value);
                                }}
                              />
                            </div>
                          </div>
                          {inputs.useScriptCache && (
                            <div className="flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4 pl-4">
                              <div className="space-y-2">
                                <div className="flex gap-2">
                                  <Label>Code Key (optional)</Label>
                                  <HelpTooltip content="A static or dynamic key for directing code generation." />
                                </div>
                                <WorkflowBlockInputTextarea
                                  nodeId={id}
                                  onChange={(value) => {
                                    const v = value.length ? value : null;
                                    handleChange("scriptCacheKey", v);
                                  }}
                                  value={inputs.scriptCacheKey ?? ""}
                                  placeholder={
                                    placeholders["scripts"]["scriptKey"]
                                  }
                                  className="nopan text-xs"
                                />
                              </div>
                              <div className="space-y-2">
                                <div className="flex items-center gap-2">
                                  <Label>Fallback To AI On Failure</Label>
                                  <HelpTooltip content="If cached code fails, fallback to AI." />
                                  <Switch
                                    className="ml-auto"
                                    checked={inputs.aiFallback}
                                    onCheckedChange={(value) => {
                                      handleChange("aiFallback", value);
                                    }}
                                  />
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      </OrgWalled>
                      <div className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Label>Save &amp; Reuse Session</Label>
                          <HelpTooltip content="Persist session information across workflow runs" />
                          <Switch
                            className="ml-auto"
                            checked={inputs.persistBrowserSession}
                            onCheckedChange={(value) => {
                              handleChange("persistBrowserSession", value);
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
                          value={inputs.extraHttpHeaders ?? null}
                          onChange={(val) =>
                            handleChange("extraHttpHeaders", val)
                          }
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
                          value={inputs.maxScreenshotScrolls ?? ""}
                          placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
                          onChange={(event) => {
                            const value =
                              event.target.value === ""
                                ? null
                                : Number(event.target.value);

                            handleChange("maxScreenshotScrolls", value);
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
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <div className="w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 text-center">
        Start
        <div className="mt-4 flex gap-3 rounded-md bg-slate-800 p-3">
          <span className="rounded bg-slate-700 p-1 text-lg">💡</span>
          <div className="space-y-1 text-left text-xs text-slate-400">
            Use{" "}
            <code className="text-white">
              &#123;&#123;&nbsp;current_value&nbsp;&#125;&#125;
            </code>{" "}
            to get the current loop value for a given iteration.
          </div>
        </div>
      </div>
    </div>
  );
}

export { StartNode };
