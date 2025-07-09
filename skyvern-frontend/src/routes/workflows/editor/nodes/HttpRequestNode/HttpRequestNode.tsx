import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import type { HttpRequestNode as HttpRequestNodeType } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Switch } from "@/components/ui/switch";
import { placeholders, helpTooltips } from "../../helpContent";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { CodeIcon, PlusIcon, MagicWandIcon } from "@radix-ui/react-icons";
import { CurlImportDialog } from "./CurlImportDialog";
import { QuickHeadersDialog } from "./QuickHeadersDialog";
import { MethodBadge, UrlValidator, RequestPreview } from "./HttpUtils";

const httpMethods = [
  "GET",
  "POST",
  "PUT",
  "DELETE",
  "PATCH",
  "HEAD",
  "OPTIONS",
];

const urlTooltip =
  "The URL to send the HTTP request to. You can use {{ parameter_name }} to reference parameters.";
const methodTooltip = "The HTTP method to use for the request.";
const headersTooltip =
  "HTTP headers to include with the request as JSON object.";
const bodyTooltip =
  "Request body as JSON object. Only used for POST, PUT, PATCH methods.";
const timeoutTooltip = "Request timeout in seconds.";
const followRedirectsTooltip =
  "Whether to automatically follow HTTP redirects.";

function HttpRequestNode({ id, data }: NodeProps<HttpRequestNodeType>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    method: data.method,
    url: data.url,
    headers: data.headers,
    body: data.body,
    timeout: data.timeout,
    followRedirects: data.followRedirects,
    continueOnFailure: data.continueOnFailure,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  const handleCurlImport = (importedData: {
    method: string;
    url: string;
    headers: string;
    body: string;
    timeout: number;
    followRedirects: boolean;
  }) => {
    const newInputs = {
      ...inputs,
      method: importedData.method,
      url: importedData.url,
      headers: importedData.headers,
      body: importedData.body,
      timeout: importedData.timeout,
      followRedirects: importedData.followRedirects,
    };
    setInputs(newInputs);
    updateNodeData(id, {
      method: importedData.method,
      url: importedData.url,
      headers: importedData.headers,
      body: importedData.body,
      timeout: importedData.timeout,
      followRedirects: importedData.followRedirects,
    });
  };

  const handleQuickHeaders = (headers: Record<string, string>) => {
    try {
      const existingHeaders = JSON.parse(inputs.headers || "{}");
      const mergedHeaders = { ...existingHeaders, ...headers };
      const newHeadersString = JSON.stringify(mergedHeaders, null, 2);
      handleChange("headers", newHeadersString);
    } catch (error) {
      // If existing headers are invalid, just use the new ones
      const newHeadersString = JSON.stringify(headers, null, 2);
      handleChange("headers", newHeadersString);
    }
  };

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  const showBodyEditor =
    inputs.method !== "GET" &&
    inputs.method !== "HEAD" &&
    inputs.method !== "DELETE";

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
      <div className="w-[36rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <header className="flex h-[2.75rem] justify-between">
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
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">HTTP Request Block</span>
            </div>
          </div>
          <div className="flex gap-2">
            {/* Quick Action Buttons */}
            <CurlImportDialog onImport={handleCurlImport}>
              <Button
                variant="outline"
                size="sm"
                className="h-8 px-2 text-xs"
                disabled={!editable}
              >
                <CodeIcon className="mr-1 h-3 w-3" />
                Import cURL
              </Button>
            </CurlImportDialog>

            <NodeActionMenu
              onDelete={() => {
                deleteNodeCallback(id);
              }}
            />
          </div>
        </header>

        <div className="space-y-4">
          {/* Method and URL Section */}
          <div className="flex gap-4">
            <div className="w-32 space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Method</Label>
                <HelpTooltip content={methodTooltip} />
              </div>
              <Select
                value={inputs.method}
                onValueChange={(value) => handleChange("method", value)}
                disabled={!editable}
              >
                <SelectTrigger className="nopan text-xs">
                  <div className="flex items-center gap-2">
                    <MethodBadge method={inputs.method} />
                  </div>
                </SelectTrigger>
                <SelectContent>
                  {httpMethods.map((method) => (
                    <SelectItem key={method} value={method}>
                      <div className="flex items-center gap-2">
                        <MethodBadge method={method} />
                        {method}
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex-1 space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">URL</Label>
                  <HelpTooltip content={urlTooltip} />
                </div>
                {isFirstWorkflowBlock ? (
                  <div className="flex justify-end text-xs text-slate-400">
                    Tip: Use the {"+"} button to add parameters!
                  </div>
                ) : null}
              </div>
              <WorkflowBlockInputTextarea
                nodeId={id}
                onChange={(value) => {
                  handleChange("url", value);
                }}
                value={inputs.url}
                placeholder={placeholders["httpRequest"]["url"]}
                className="nopan text-xs"
              />
              <UrlValidator url={inputs.url} />
            </div>
          </div>

          {/* Headers Section */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Headers</Label>
                <HelpTooltip content={headersTooltip} />
              </div>
              <QuickHeadersDialog onAdd={handleQuickHeaders}>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={!editable}
                >
                  <PlusIcon className="mr-1 h-3 w-3" />
                  Quick Headers
                </Button>
              </QuickHeadersDialog>
            </div>
            <CodeEditor
              className="w-full"
              language="json"
              value={inputs.headers}
              onChange={(value) => {
                handleChange("headers", value || "{}");
              }}
              readOnly={!editable}
              minHeight="80px"
              maxHeight="160px"
            />
          </div>

          {/* Body Section */}
          {showBodyEditor && (
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Body</Label>
                <HelpTooltip content={bodyTooltip} />
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={inputs.body}
                onChange={(value) => {
                  handleChange("body", value || "{}");
                }}
                readOnly={!editable}
                minHeight="100px"
                maxHeight="200px"
              />
            </div>
          )}

          {/* Request Preview */}
          <RequestPreview
            method={inputs.method}
            url={inputs.url}
            headers={inputs.headers}
            body={inputs.body}
          />
        </div>

        <Separator />

        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) => {
                    updateNodeData(id, { parameterKeys });
                  }}
                />
                <div className="flex gap-4">
                  <div className="w-32 space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">Timeout</Label>
                      <HelpTooltip content={timeoutTooltip} />
                    </div>
                    <Input
                      type="number"
                      min="1"
                      max="300"
                      value={inputs.timeout}
                      onChange={(e) =>
                        handleChange("timeout", parseInt(e.target.value) || 30)
                      }
                      className="nopan text-xs"
                      disabled={!editable}
                    />
                  </div>
                  <div className="flex-1 space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Follow Redirects
                      </Label>
                      <HelpTooltip content={followRedirectsTooltip} />
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-slate-400">
                        Automatically follow HTTP redirects
                      </span>
                      <Switch
                        checked={inputs.followRedirects}
                        onCheckedChange={(checked) =>
                          handleChange("followRedirects", checked)
                        }
                        disabled={!editable}
                      />
                    </div>
                  </div>
                  <div className="flex-1 space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Continue on Failure
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["httpRequest"]["continueOnFailure"]
                        }
                      />
                    </div>
                    <div className="flex items-center justify-end">
                      <Switch
                        checked={inputs.continueOnFailure}
                        onCheckedChange={(checked) =>
                          handleChange("continueOnFailure", checked)
                        }
                        disabled={!editable}
                      />
                    </div>
                  </div>
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>

        {/* Tips Section */}
        <div className="rounded-md bg-slate-800/50 p-3">
          <div className="space-y-2 text-xs text-slate-400">
            <div className="flex items-center gap-2">
              <MagicWandIcon className="h-3 w-3" />
              <span className="font-medium">Quick Tips:</span>
            </div>
            <ul className="ml-5 list-disc space-y-1">
              <li>
                Use "Import cURL" to quickly convert API documentation examples
              </li>
              <li>
                Use "Quick Headers" in the headers section to add common
                authentication and content headers
              </li>
              <li>
                The request will return response data including status, headers,
                and body
              </li>
              <li>Reference response data in later blocks with parameters</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

export { HttpRequestNode };
