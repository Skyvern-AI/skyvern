import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { type HttpRequestNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Plus, Trash2 } from "lucide-react";

function HttpRequestNode({ id, data }: NodeProps<HttpRequestNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const [inputs, setInputs] = useState({
    inputMode: data.inputMode || "manual",
    curlCommand: data.curlCommand || "",
    method: data.method || "GET",
    url: data.url || "",
    headers: data.headers || {},
    body: data.body || "",
    timeout: data.timeout || 30,
    followRedirects: data.followRedirects ?? true,
  });

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  function addHeader() {
    const newHeaders = { ...inputs.headers, "": "" };
    handleChange("headers", newHeaders);
  }

  function updateHeader(oldKey: string, newKey: string, value: string) {
    const newHeaders = { ...inputs.headers };
    if (oldKey !== newKey) {
      delete newHeaders[oldKey];
    }
    newHeaders[newKey] = value;
    handleChange("headers", newHeaders);
  }

  function removeHeader(key: string) {
    const newHeaders = { ...inputs.headers };
    delete newHeaders[key];
    handleChange("headers", newHeaders);
  }

  const httpMethods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"];

  return (
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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.HttpRequest}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">HTTP Request Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>

        <Tabs value={inputs.inputMode} onValueChange={(value) => handleChange("inputMode", value)}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="manual">Manual</TabsTrigger>
            <TabsTrigger value="curl">Curl Command</TabsTrigger>
          </TabsList>

          <TabsContent value="curl" className="space-y-4">
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Curl Command</Label>
                <HelpTooltip content="Paste a curl command from your browser's developer tools" />
              </div>
              <WorkflowBlockInputTextarea
                nodeId={id}
                onChange={(value) => handleChange("curlCommand", value)}
                value={inputs.curlCommand}
                placeholder="curl 'https://api.example.com/data' -H 'Authorization: Bearer token'"
                className="nopan min-h-[100px] text-xs font-mono"
              />
            </div>
          </TabsContent>

          <TabsContent value="manual" className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="text-xs text-slate-300">Method</Label>
                <Select
                  value={inputs.method}
                  onValueChange={(value) => handleChange("method", value)}
                >
                  <SelectTrigger className="nopan w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {httpMethods.map((method) => (
                      <SelectItem key={method} value={method}>
                        {method}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label className="text-xs text-slate-300">Timeout (seconds)</Label>
                <Input
                  type="number"
                  value={inputs.timeout}
                  onChange={(e) => handleChange("timeout", parseInt(e.target.value) || 30)}
                  placeholder="30"
                  className="nopan"
                  min={1}
                  max={300}
                />
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">URL</Label>
                <HelpTooltip content="The URL to send the request to. You can use template variables like {{ variable_name }}" />
              </div>
              <Input
                value={inputs.url}
                onChange={(e) => handleChange("url", e.target.value)}
                placeholder="https://api.example.com/endpoint"
                className="nopan text-xs"
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Headers</Label>
                  <HelpTooltip content="HTTP headers to send with the request" />
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={addHeader}
                  className="h-6 px-2"
                >
                  <Plus className="h-3 w-3 mr-1" />
                  Add Header
                </Button>
              </div>
              <div className="space-y-2">
                {Object.entries(inputs.headers).map(([key, value], index) => (
                  <div key={index} className="flex gap-2">
                    <Input
                      value={key}
                      onChange={(e) => updateHeader(key, e.target.value, value)}
                      placeholder="Header Name"
                      className="nopan text-xs"
                    />
                    <Input
                      value={value}
                      onChange={(e) => updateHeader(key, key, e.target.value)}
                      placeholder="Header Value"
                      className="nopan text-xs"
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => removeHeader(key)}
                      className="h-8 w-8 p-0"
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </div>
                ))}
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Body</Label>
                <HelpTooltip content="Request body. Can be JSON, form data, or plain text" />
              </div>
              <WorkflowBlockInputTextarea
                nodeId={id}
                onChange={(value) => handleChange("body", value)}
                value={typeof inputs.body === "string" ? inputs.body : JSON.stringify(inputs.body, null, 2)}
                placeholder='{"key": "value"}'
                className="nopan min-h-[100px] text-xs font-mono"
              />
            </div>

            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Follow Redirects</Label>
                <HelpTooltip content="Whether to automatically follow HTTP redirects" />
              </div>
              <Switch
                checked={inputs.followRedirects}
                onCheckedChange={(checked) => handleChange("followRedirects", checked)}
                className="nopan"
              />
            </div>
          </TabsContent>
        </Tabs>

        <Separator />

        <div className="space-y-2">
          <ParametersMultiSelect
            availableOutputParameters={outputParameterKeys}
            parameters={data.parameterKeys}
            onParametersChange={(parameterKeys) => {
              updateNodeData(id, { parameterKeys });
            }}
          />
        </div>
      </div>
    </div>
  );
}

export { HttpRequestNode };