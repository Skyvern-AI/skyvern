import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { StartNode } from "./types";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { useState } from "react";
import { ProxyLocation } from "@/api/types";
import { Label } from "@/components/ui/label";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";

function StartNode({ id, data }: NodeProps<StartNode>) {
  const { updateNodeData } = useReactFlow();
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
  });

  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  if (data.withWorkflowSettings) {
    return (
      <div>
        <Handle
          type="source"
          position={Position.Bottom}
          id="a"
          className="opacity-0"
        />
        <div className="w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 text-center">
          <div className="space-y-4">
            <header>Start</header>
            <Separator />
            <Accordion type="single" collapsible>
              <AccordionItem value="settings" className="border-b-0">
                <AccordionTrigger className="py-2">
                  Workflow Settings
                </AccordionTrigger>
                <AccordionContent className="pl-6 pr-1 pt-1">
                  <div className="space-y-4">
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
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <Label>Persist Browser Session</Label>
                        <HelpTooltip content="Persist session information across workflow runs" />
                        <Switch
                          checked={inputs.persistBrowserSession}
                          onCheckedChange={(value) => {
                            handleChange("persistBrowserSession", value);
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>
        </div>
      </div>
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
      </div>
    </div>
  );
}

export { StartNode };
