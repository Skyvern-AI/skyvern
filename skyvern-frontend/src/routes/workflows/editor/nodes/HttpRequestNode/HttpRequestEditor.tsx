import { CodeIcon, MagicWandIcon, PlusIcon } from "@radix-ui/react-icons";
import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useCallback } from "react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

import { helpTooltips, placeholders } from "../../helpContent";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { type AppNode } from "..";
import { CurlImportDialog } from "./CurlImportDialog";
import {
  JsonValidator,
  MethodBadge,
  RequestPreview,
  UrlValidator,
} from "./HttpUtils";
import { QuickHeadersDialog } from "./QuickHeadersDialog";
import { type HttpRequestNode, type HttpRequestNodeData } from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowBlockParameterSelect } from "../WorkflowBlockParameterSelect";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";

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
const downloadFilenameTooltip =
  "The complete filename (without extension) for downloaded files. Extension is automatically determined from the response Content-Type.";

function HttpRequestEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside the
  // per-node renderer and the body subscribes to useNodes()/useEdges() for
  // output-parameter discovery; a one-time getNode() snapshot would re-render
  // with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<HttpRequestNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "http_request") {
    return null;
  }
  return <HttpRequestEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function HttpRequestEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: HttpRequestNodeData;
}) {
  const {
    editable,
    method,
    url,
    headers,
    body,
    files,
    timeout,
    followRedirects,
    parameterKeys,
    downloadFilename,
    saveResponseAsFile,
    continueOnFailure,
  } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const update = useUpdate<HttpRequestNodeData>({ id: blockId, editable });
  const hasInteracted = useHasInteractedThisSession();

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
    (next: Record<string, string>) => {
      try {
        const existingHeaders = JSON.parse(headers || "{}");
        const mergedHeaders = { ...existingHeaders, ...next };
        update({ headers: JSON.stringify(mergedHeaders, null, 2) });
      } catch {
        update({ headers: JSON.stringify(next, null, 2) });
      }
    },
    [headers, update],
  );

  const showBodyEditor =
    method !== "GET" && method !== "HEAD" && method !== "DELETE";

  const handleAddParameterToBody = useCallback(
    (parameterKey: string) => {
      const parameterSyntax = `{{ ${parameterKey} }}`;
      const currentBody = body || "{}";
      try {
        const parsed = JSON.parse(currentBody);
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
        update({ body: JSON.stringify({ param_1: parameterSyntax }, null, 2) });
      }
    },
    [body, update],
  );

  return (
    <div data-testid="http-request-block-form" className="space-y-4">
      <div>
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
      </div>

      <div className="space-y-4">
        <div className="flex gap-4">
          <div className="w-32 space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Method</Label>
              <HelpTooltip content={methodTooltip} />
            </div>
            <Select
              value={method}
              onValueChange={(next) => update({ method: next })}
              disabled={!editable}
            >
              <SelectTrigger className="nopan text-xs">
                <div className="flex items-center gap-2">
                  <MethodBadge method={method} />
                </div>
              </SelectTrigger>
              <SelectContent>
                {httpMethods.map((m) => (
                  <SelectItem key={m} value={m}>
                    <div className="flex items-center gap-2">
                      <MethodBadge method={m} />
                      {m}
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex-1 space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">URL</Label>
              <HelpTooltip content={urlTooltip} />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(next) => update({ url: next })}
              value={url}
              placeholder={placeholders["httpRequest"]["url"]}
              className="nopan text-xs"
            />
            <UrlValidator url={url} />
          </div>
        </div>

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
            value={headers}
            onChange={(next) => update({ headers: next || "{}" })}
            readOnly={!editable}
            minHeight="80px"
            maxHeight="160px"
          />
          <JsonValidator value={headers} />
        </div>

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
                    nodeId={blockId}
                    onAdd={handleAddParameterToBody}
                  />
                </PopoverContent>
              </Popover>
            </div>
            <CodeEditor
              className="w-full"
              language="json"
              value={body}
              onChange={(next) => update({ body: next || "{}" })}
              readOnly={!editable}
              minHeight="100px"
              maxHeight="200px"
            />
            <JsonValidator value={body} />
          </div>
        )}

        {showBodyEditor && (
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Files</Label>
              <HelpTooltip content={filesTooltip} />
            </div>
            <CodeEditor
              className="w-full"
              language="json"
              value={files}
              onChange={(next) => update({ files: next || "{}" })}
              readOnly={!editable}
              minHeight="80px"
              maxHeight="160px"
            />
            <JsonValidator value={files} />
          </div>
        )}

        <RequestPreview
          method={method}
          url={url}
          headers={headers}
          body={body}
          files={files}
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
                parameters={parameterKeys}
                onParametersChange={(next) => update({ parameterKeys: next })}
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
                    value={timeout}
                    onChange={(event) =>
                      update({ timeout: parseInt(event.target.value) || 30 })
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
                      checked={followRedirects}
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
                      content={helpTooltips["httpRequest"]["continueOnFailure"]}
                    />
                  </div>
                  <div className="flex items-center justify-end">
                    <Switch
                      checked={continueOnFailure}
                      onCheckedChange={(checked) =>
                        update({ continueOnFailure: checked })
                      }
                      disabled={!editable}
                    />
                  </div>
                </div>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Save Response as File
                    </Label>
                    <HelpTooltip content="When enabled, the response body will be saved as a file instead of being parsed as JSON/text." />
                  </div>
                  <Switch
                    checked={saveResponseAsFile}
                    onCheckedChange={(checked) =>
                      update({ saveResponseAsFile: checked })
                    }
                    disabled={!editable}
                  />
                </div>
                {saveResponseAsFile && (
                  <div className="space-y-2 border-l-2 border-slate-600 pl-4">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Download Filename
                      </Label>
                      <HelpTooltip content={downloadFilenameTooltip} />
                    </div>
                    <Input
                      type="text"
                      value={downloadFilename}
                      onChange={(event) =>
                        update({ downloadFilename: event.target.value })
                      }
                      placeholder="Auto-generated from URL"
                      className="nopan text-xs"
                      disabled={!editable}
                    />
                  </div>
                )}
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>

      {!hasInteracted && (
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
      )}
    </div>
  );
}

export { HttpRequestEditor };
