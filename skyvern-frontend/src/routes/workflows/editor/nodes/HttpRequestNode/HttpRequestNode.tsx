import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { useCallback } from "react";
import { NodeHeader } from "../components/NodeHeader";
import { NodeTabs } from "../components/NodeTabs";
import type { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";
import type { HttpRequestNode as HttpRequestNodeType } from "./types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Switch } from "@/components/ui/switch";
import { placeholders, helpTooltips } from "../../helpContent";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { CodeIcon, PlusIcon, MagicWandIcon } from "@radix-ui/react-icons";
import { WorkflowBlockParameterSelect } from "../WorkflowBlockParameterSelect";
import { CurlImportDialog } from "./CurlImportDialog";
import { QuickHeadersDialog } from "./QuickHeadersDialog";
import { MethodBadge, UrlValidator, RequestPreview } from "./HttpUtils";
import { useRerender } from "@/hooks/useRerender";
import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";

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
const filesTooltip =
  'Files to upload as multipart/form-data. Dictionary mapping field names to file paths/URLs. Supports HTTP/HTTPS URLs, S3 URIs (s3://), Azure blob URIs (azure://), or limited local file access. Example: {"file": "https://example.com/file.pdf"} or {"document": "s3://bucket/path/file.pdf"}';
const timeoutTooltip = "Request timeout in seconds.";
const followRedirectsTooltip =
  "Whether to automatically follow HTTP redirects.";

function HttpRequestNode({ id, data, type }: NodeProps<HttpRequestNodeType>) {
  const { editable } = data;
  const [label] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const rerender = useRerender({ prefix: "accordian" });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const update = useUpdate<HttpRequestNodeType["data"]>({ id, editable });

  const handleCurlImport = useCallback(
    (importedData: {
      method: string;
      url: string;
      headers: string;
      body: string;
      timeout: number;
      followRedirects: boolean;
    }) => {
      update({
        method: importedData.method,
        url: importedData.url,
        headers: importedData.headers,
        body: importedData.body,
        timeout: importedData.timeout,
        followRedirects: importedData.followRedirects,
      });
    },
    [update],
  );

  const handleQuickHeaders = useCallback(
    (headers: Record<string, string>) => {
      try {
        const existingHeaders = JSON.parse(data.headers || "{}");
        const mergedHeaders = { ...existingHeaders, ...headers };
        const newHeadersString = JSON.stringify(mergedHeaders, null, 2);
        update({ headers: newHeadersString });
      } catch (error) {
        // If existing headers are invalid, just use the new ones
        const newHeadersString = JSON.stringify(headers, null, 2);
        update({ headers: newHeadersString });
      }
    },
    [data.headers, update],
  );

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const recordingStore = useRecordingStore();

  const showBodyEditor =
    data.method !== "GET" && data.method !== "HEAD" && data.method !== "DELETE";

  const handleAddParameterToBody = useCallback(
    (parameterKey: string) => {
      const parameterSyntax = `{{ ${parameterKey} }}`;
      const currentBody = data.body || "{}";
      try {
        const parsed = JSON.parse(currentBody);
        // Add as a new field with unique key
        const existingKeys = Object.keys(parsed);
        let keyIndex = existingKeys.length + 1;
        let newKey = `param_${keyIndex}`;
        while (existingKeys.includes(newKey)) {
          keyIndex++;
          newKey = `param_${keyIndex}`;
        }
        parsed[newKey] = parameterSyntax;
        update({ body: JSON.stringify(parsed, null, 2) });
      } catch {
        // If invalid JSON, reset to valid JSON with the parameter
        update({ body: JSON.stringify({ param_1: parameterSyntax }, null, 2) });
      }
    },
    [data.body, update],
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
      <div className="w-[36rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type={type as WorkflowBlockType}
          extraActions={
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
          }
        />

        <div className="space-y-4">
          {/* Method and URL Section */}
          <div className="flex gap-4">
            <div className="w-32 space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Method</Label>
                <HelpTooltip content={methodTooltip} />
              </div>
              <Select
                value={data.method}
                onValueChange={(value) => update({ method: value })}
                disabled={!editable}
              >
                <SelectTrigger className="nopan text-xs">
                  <div className="flex items-center gap-2">
                    <MethodBadge method={data.method} />
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
                canWriteTitle={true}
                nodeId={id}
                onChange={(value) => {
                  update({ url: value });
                }}
                value={data.url}
                placeholder={placeholders["httpRequest"]["url"]}
                className="nopan text-xs"
              />
              <UrlValidator url={data.url} />
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
              value={data.headers}
              onChange={(value) => {
                update({ headers: value || "{}" });
              }}
              readOnly={!editable}
              minHeight="80px"
              maxHeight="160px"
            />
          </div>

          {/* Body Section */}
          {showBodyEditor && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Body</Label>
                  <HelpTooltip content={bodyTooltip} />
                </div>
                <Popover>
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      disabled={!editable}
                    >
                      <PlusIcon className="mr-1 h-3 w-3" />
                      Add Parameter
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent className="w-[22rem]">
                    <WorkflowBlockParameterSelect
                      nodeId={id}
                      onAdd={handleAddParameterToBody}
                    />
                  </PopoverContent>
                </Popover>
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={data.body}
                onChange={(value) => {
                  update({ body: value || "{}" });
                }}
                readOnly={!editable}
                minHeight="100px"
                maxHeight="200px"
              />
            </div>
          )}

          {/* Files Section */}
          {showBodyEditor && (
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Files</Label>
                <HelpTooltip content={filesTooltip} />
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={data.files}
                onChange={(value) => {
                  update({ files: value || "{}" });
                }}
                readOnly={!editable}
                minHeight="80px"
                maxHeight="160px"
              />
            </div>
          )}

          {/* Request Preview */}
          <RequestPreview
            method={data.method}
            url={data.url}
            headers={data.headers}
            body={data.body}
            files={data.files}
          />
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
            <AccordionContent key={rerender.key} className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) => {
                    update({ parameterKeys });
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
                      value={data.timeout}
                      onChange={(e) =>
                        update({
                          timeout: parseInt(e.target.value) || 30,
                        })
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
                        checked={data.followRedirects}
                        onCheckedChange={(checked) =>
                          update({ followRedirects: checked })
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
                        checked={data.continueOnFailure}
                        onCheckedChange={(checked) =>
                          update({ continueOnFailure: checked })
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
                Password credential: {"{{ my_credential.username }}"} /{" "}
                {"{{ my_credential.password }}"}
              </li>
              <li>Secret credential: {"{{ my_secret.secret_value }}"}</li>
              <li>
                The request will return response data including status, headers,
                and body
              </li>
              <li>Reference response data in later blocks with parameters</li>
            </ul>
          </div>
        </div>

        <NodeTabs blockLabel={label} />
      </div>
    </div>
  );
}

export { HttpRequestNode };
