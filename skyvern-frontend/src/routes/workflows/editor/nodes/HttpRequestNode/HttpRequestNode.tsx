import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
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
import { useState, useCallback, useMemo, useEffect } from "react";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { type HttpRequestNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { 
  Plus, 
  Trash2, 
  AlertCircle, 
  Info, 
  Copy, 
  Check,
  ChevronDown,
  ChevronUp,
  Code,
  FileJson,
  Globe,
  Clock,
  RefreshCw
} from "lucide-react";
import { cn } from "@/lib/utils";
import { parseCurlCommand } from "./curlParser";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

// Common headers for autocomplete
const COMMON_HEADERS = [
  "Content-Type",
  "Authorization",
  "Accept",
  "User-Agent",
  "Cache-Control",
  "Cookie",
  "Origin",
  "Referer",
  "X-Requested-With",
  "X-API-Key",
];

const CONTENT_TYPE_OPTIONS = [
  "application/json",
  "application/x-www-form-urlencoded",
  "multipart/form-data",
  "text/plain",
  "text/html",
  "application/xml",
];

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

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [curlError, setCurlError] = useState<string | null>(null);
  const [urlError, setUrlError] = useState<string | null>(null);
  const [bodyError, setBodyError] = useState<string | null>(null);
  const [copiedCurl, setCopiedCurl] = useState(false);

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  // Validate URL
  useEffect(() => {
    if (inputs.url && inputs.inputMode === "manual") {
      try {
        new URL(inputs.url);
        setUrlError(null);
      } catch {
        if (!inputs.url.includes("{{")) {
          setUrlError("Invalid URL format");
        } else {
          setUrlError(null); // Allow template variables
        }
      }
    } else {
      setUrlError(null);
    }
  }, [inputs.url, inputs.inputMode]);

  // Validate JSON body
  useEffect(() => {
    if (inputs.body && typeof inputs.body === "string" && inputs.inputMode === "manual") {
      const trimmed = inputs.body.trim();
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        try {
          JSON.parse(trimmed);
          setBodyError(null);
        } catch {
          if (!trimmed.includes("{{")) {
            setBodyError("Invalid JSON format");
          } else {
            setBodyError(null); // Allow template variables
          }
        }
      } else {
        setBodyError(null);
      }
    } else {
      setBodyError(null);
    }
  }, [inputs.body, inputs.inputMode]);

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  const parseCurl = useCallback(() => {
    try {
      const parsed = parseCurlCommand(inputs.curlCommand);
      setInputs({
        ...inputs,
        method: parsed.method,
        url: parsed.url,
        headers: parsed.headers,
        body: parsed.body,
        inputMode: "manual",
      });
      updateNodeData(id, {
        method: parsed.method,
        url: parsed.url,
        headers: parsed.headers,
        body: parsed.body,
        inputMode: "manual",
      });
      setCurlError(null);
    } catch (error) {
      setCurlError(error instanceof Error ? error.message : "Failed to parse curl command");
    }
  }, [inputs.curlCommand, id, updateNodeData]);

  const generateCurl = useCallback(() => {
    let curl = `curl -X ${inputs.method}`;
    
    // Add URL
    curl += ` '${inputs.url}'`;
    
    // Add headers
    Object.entries(inputs.headers).forEach(([key, value]) => {
      if (key && value) {
        curl += ` \\\n  -H '${key}: ${value}'`;
      }
    });
    
    // Add body
    if (inputs.body && ["POST", "PUT", "PATCH"].includes(inputs.method)) {
      const bodyStr = typeof inputs.body === "string" ? inputs.body : JSON.stringify(inputs.body);
      curl += ` \\\n  -d '${bodyStr.replace(/'/g, "\\'")}'`;
    }
    
    return curl;
  }, [inputs]);

  const copyCurlToClipboard = useCallback(() => {
    const curl = generateCurl();
    navigator.clipboard.writeText(curl);
    setCopiedCurl(true);
    setTimeout(() => setCopiedCurl(false), 2000);
  }, [generateCurl]);

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

  function addCommonHeader(headerName: string) {
    const newHeaders = { ...inputs.headers };
    if (!newHeaders[headerName]) {
      newHeaders[headerName] = headerName === "Content-Type" ? "application/json" : "";
      handleChange("headers", newHeaders);
    }
  }

  const httpMethods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"];
  
  const methodColors = {
    GET: "bg-blue-500",
    POST: "bg-green-500",
    PUT: "bg-yellow-500",
    DELETE: "bg-red-500",
    PATCH: "bg-purple-500",
    HEAD: "bg-gray-500",
    OPTIONS: "bg-indigo-500",
  };

  const hasBody = ["POST", "PUT", "PATCH"].includes(inputs.method);

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
      <div className="w-[35rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
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
            <TabsTrigger value="manual" className="flex items-center gap-2">
              <Globe className="h-3 w-3" />
              Manual
            </TabsTrigger>
            <TabsTrigger value="curl" className="flex items-center gap-2">
              <Code className="h-3 w-3" />
              Import cURL
            </TabsTrigger>
          </TabsList>

          <TabsContent value="curl" className="space-y-4 mt-4">
            <Alert>
              <Info className="h-4 w-4" />
              <AlertDescription>
                Paste a cURL command from your browser's developer tools to automatically configure the request
              </AlertDescription>
            </Alert>
            
            <div className="space-y-2">
              <div className="flex justify-between items-center">
                <Label className="text-xs text-slate-300">cURL Command</Label>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={parseCurl}
                    disabled={!inputs.curlCommand}
                    className="h-7 text-xs"
                  >
                    Parse & Convert
                  </Button>
                </div>
              </div>
              <Textarea
                value={inputs.curlCommand}
                onChange={(e) => handleChange("curlCommand", e.target.value)}
                placeholder="curl 'https://api.example.com/data' -H 'Authorization: Bearer token'"
                className="min-h-[120px] font-mono text-xs"
              />
              {curlError && (
                <Alert variant="destructive" className="mt-2">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{curlError}</AlertDescription>
                </Alert>
              )}
            </div>
          </TabsContent>

          <TabsContent value="manual" className="space-y-4 mt-4">
            {/* Method and URL */}
            <div className="flex gap-2">
              <Select
                value={inputs.method}
                onValueChange={(value) => handleChange("method", value)}
              >
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {httpMethods.map((method) => (
                    <SelectItem key={method} value={method}>
                      <div className="flex items-center gap-2">
                        <div className={cn(
                          "w-2 h-2 rounded-full",
                          methodColors[method as keyof typeof methodColors]
                        )} />
                        {method}
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              
              <div className="flex-1 space-y-1">
                <Input
                  value={inputs.url}
                  onChange={(e) => handleChange("url", e.target.value)}
                  placeholder="https://api.example.com/endpoint"
                  className={cn("text-xs", urlError && "border-red-500")}
                />
                {urlError && (
                  <p className="text-xs text-red-500">{urlError}</p>
                )}
              </div>
            </div>

            {/* Headers Section */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Label className="text-xs text-slate-300">Headers</Label>
                  <Badge variant="secondary" className="text-xs">
                    {Object.keys(inputs.headers).filter(k => k).length}
                  </Badge>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={addHeader}
                  className="h-7 text-xs"
                >
                  <Plus className="h-3 w-3 mr-1" />
                  Add Header
                </Button>
              </div>

              {Object.keys(inputs.headers).length > 0 && (
                <Card className="border-slate-700 bg-slate-elevation2">
                  <CardContent className="p-3 space-y-2">
                    {Object.entries(inputs.headers).map(([key, value], index) => (
                      <div key={index} className="flex gap-2">
                        <Input
                          value={key}
                          onChange={(e) => updateHeader(key, e.target.value, value)}
                          placeholder="Header Name"
                          className="text-xs"
                          list={`headers-${id}`}
                        />
                        <Input
                          value={value}
                          onChange={(e) => updateHeader(key, key, e.target.value)}
                          placeholder="Header Value"
                          className="text-xs"
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
                  </CardContent>
                </Card>
              )}
              
              <datalist id={`headers-${id}`}>
                {COMMON_HEADERS.map((header) => (
                  <option key={header} value={header} />
                ))}
              </datalist>

              <div className="flex flex-wrap gap-1">
                {COMMON_HEADERS.filter(h => !inputs.headers[h]).slice(0, 5).map((header) => (
                  <Button
                    key={header}
                    variant="ghost"
                    size="sm"
                    onClick={() => addCommonHeader(header)}
                    className="h-6 text-xs px-2"
                  >
                    <Plus className="h-3 w-3 mr-1" />
                    {header}
                  </Button>
                ))}
              </div>
            </div>

            {/* Body Section */}
            {hasBody && (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Label className="text-xs text-slate-300">Body</Label>
                    <HelpTooltip content="Request body. Supports JSON, form data, or plain text. Use {{ }} for template variables." />
                  </div>
                  <Select
                    value={inputs.headers["Content-Type"] || "application/json"}
                    onValueChange={(value) => updateHeader("Content-Type", "Content-Type", value)}
                  >
                    <SelectTrigger className="w-48 h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {CONTENT_TYPE_OPTIONS.map((type) => (
                        <SelectItem key={type} value={type} className="text-xs">
                          {type}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => handleChange("body", value)}
                  value={typeof inputs.body === "string" ? inputs.body : JSON.stringify(inputs.body, null, 2)}
                  placeholder={inputs.headers["Content-Type"]?.includes("json") 
                    ? '{\n  "key": "value"\n}' 
                    : "Request body content..."}
                  className={cn("nopan min-h-[120px] text-xs font-mono", bodyError && "border-red-500")}
                />
                {bodyError && (
                  <p className="text-xs text-red-500">{bodyError}</p>
                )}
              </div>
            )}

            {/* Advanced Settings */}
            <Collapsible open={showAdvanced} onOpenChange={setShowAdvanced}>
              <CollapsibleTrigger asChild>
                <Button variant="ghost" className="w-full justify-between h-8 text-xs">
                  Advanced Settings
                  {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent className="space-y-4 mt-2">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <Clock className="h-3 w-3 text-slate-400" />
                      <Label className="text-xs text-slate-300">Timeout (seconds)</Label>
                    </div>
                    <Input
                      type="number"
                      value={inputs.timeout}
                      onChange={(e) => handleChange("timeout", parseInt(e.target.value) || 30)}
                      placeholder="30"
                      className="text-xs"
                      min={1}
                      max={300}
                    />
                  </div>

                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <RefreshCw className="h-3 w-3 text-slate-400" />
                        <Label className="text-xs text-slate-300">Follow Redirects</Label>
                      </div>
                      <Switch
                        checked={inputs.followRedirects}
                        onCheckedChange={(checked) => handleChange("followRedirects", checked)}
                      />
                    </div>
                  </div>
                </div>

                <div className="flex items-center justify-between p-3 bg-slate-elevation2 rounded-md">
                  <span className="text-xs text-slate-400">Export as cURL</span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={copyCurlToClipboard}
                    className="h-7 text-xs"
                  >
                    {copiedCurl ? (
                      <>
                        <Check className="h-3 w-3 mr-1" />
                        Copied!
                      </>
                    ) : (
                      <>
                        <Copy className="h-3 w-3 mr-1" />
                        Copy cURL
                      </>
                    )}
                  </Button>
                </div>
              </CollapsibleContent>
            </Collapsible>
          </TabsContent>
        </Tabs>

        <Separator />

        {/* Parameters Section */}
        <div className="space-y-2">
          <ParametersMultiSelect
            availableOutputParameters={outputParameterKeys}
            parameters={data.parameterKeys}
            onParametersChange={(parameterKeys) => {
              updateNodeData(id, { parameterKeys });
            }}
          />
        </div>

        {/* Output Preview */}
        <Card className="border-slate-700 bg-slate-elevation2">
          <CardContent className="p-3">
            <div className="flex items-center gap-2 mb-2">
              <FileJson className="h-4 w-4 text-slate-400" />
              <span className="text-xs text-slate-400">Output Structure</span>
            </div>
            <pre className="text-xs text-slate-500 font-mono">
{`{
  "status_code": 200,
  "headers": { ... },
  "url": "${inputs.url || "https://..."}",
  "body": { ... }
}`}
            </pre>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

export { HttpRequestNode };