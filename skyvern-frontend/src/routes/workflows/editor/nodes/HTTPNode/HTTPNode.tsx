import { useApolloClient } from "@apollo/client";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useCallback, useState } from "react";
import { Node } from "../types";
import type { HTTPNode, HTTPNodeData } from "./types";
import { CodeEditor } from "@/components/CodeEditor";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiBaseUrl } from "@/util/env";
import { stringify as convertToYAML } from "yaml";
import { toast } from "@/hooks/use-toast";
import { NodeActionMenu } from "../NodeActionMenu";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { Separator } from "@/components/ui/separator";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { WorkflowBlockParameterSelect } from "../WorkflowBlockParameterSelect";
import { helpTooltips } from "../../helpContent";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/util/utils";

function HTTPNode({ id, data }: NodeProps<HTTPNode>) {
  const client = useApolloClient();
  const { workflowPermanentId } = useParams();
  const { getNode, updateNodeData } = useReactFlow();
  const [content, setContent] = useState<string>(
    data.curlCommand ?? "curl https://api.example.com"
  );
  const deleteNodeCallback = useDeleteNodeCallback();

  const [showAdvanced, setShowAdvanced] = useState(false);

  const updateNodeField = useCallback(
    (field: keyof HTTPNodeData, value: unknown) => {
      const node = getNode(id) as Node | undefined;
      if (!node) {
        return;
      }
      const newData = {
        ...node.data,
        [field]: value,
      };
      updateNodeData(id, newData);
    },
    [getNode, id, updateNodeData],
  );

  const handleCopyNode = useCallback(async () => {
    const node = getNode(id);
    if (!node) {
      return;
    }
    
    const { url } = data;
    const label = data.label === "" ? undefined : data.label;
    const parameterKeys = data.parameterKeys ?? [];
    const yaml = convertToYAML({
      label,
      curlCommand: data.curlCommand,
      method: data.method,
      url,
      headers: data.headers,
      body: data.body,
      timeout: data.timeout,
      parameterKeys: parameterKeys.length > 0 ? parameterKeys : undefined,
      continueOnFailure: data.continueOnFailure,
    });
    await navigator.clipboard.writeText(yaml);
    toast({
      title: "Copied to clipboard",
      description: "HTTP node copied as YAML",
    });
  }, [data, getNode, id]);

  const placeholder = workflowPermanentId ? undefined : "Empty";

  return (
    <>
      <Handle
        type="target"
        position={Position.Top}
        className="opacity-0"
        style={{ width: "50%", height: "50%" }}
      />
      <div className="rounded-lg border-2 border-slate-300 bg-slate-900 px-6 py-4">
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <WorkflowBlockIcon
                workflowBlockType="http_request"
                className="size-4"
              />
              <h3 className="text-xs font-bold uppercase">HTTP Request</h3>
            </div>
            <NodeActionMenu
              onCopyNode={handleCopyNode}
              onDeleteNode={() => {
                deleteNodeCallback(id);
              }}
            />
          </div>
          {data.label && (
            <>
              <div className="flex items-start justify-between gap-8">
                <Label className="font-bold text-slate-200">
                  {data.label}
                </Label>
                <Switch
                  checked={data.continueOnFailure}
                  onCheckedChange={(checked) => {
                    updateNodeField("continueOnFailure", checked);
                  }}
                  disabled={!data.editable}
                />
              </div>
              <Separator />
            </>
          )}
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs font-bold text-slate-300">
                cURL Command
              </Label>
              <HelpTooltip content={helpTooltips["httpCurl"]} />
            </div>
            <div
              className={cn(
                "min-w-96",
                content.split("\n").length > 5 && "min-h-32"
              )}
            >
              <CodeEditor
                value={content}
                onChange={(value) => {
                  setContent(value);
                }}
                onBlur={() => {
                  updateNodeField("curlCommand", content);
                }}
                language="shell"
                placeholder={placeholder}
                readOnly={!data.editable}
                className="nodrag nopan nowheel"
              />
            </div>
          </div>
          <div className="space-y-4">
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              {showAdvanced ? "Hide" : "Show"} Advanced Options
            </button>
            {showAdvanced && (
              <>
                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-300">
                    Method (Optional)
                  </Label>
                  <Input
                    value={data.method ?? ""}
                    onChange={(e) => {
                      updateNodeField("method", e.target.value || undefined);
                    }}
                    placeholder="GET, POST, PUT, DELETE..."
                    readOnly={!data.editable}
                    className="nopan"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-300">
                    URL (Optional)
                  </Label>
                  <Input
                    value={data.url ?? ""}
                    onChange={(e) => {
                      updateNodeField("url", e.target.value || undefined);
                    }}
                    placeholder="https://api.example.com/endpoint"
                    readOnly={!data.editable}
                    className="nopan"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-300">
                    Body (Optional)
                  </Label>
                  <Textarea
                    value={data.body ?? ""}
                    onChange={(e) => {
                      updateNodeField("body", e.target.value || undefined);
                    }}
                    placeholder='{"key": "value"}'
                    readOnly={!data.editable}
                    className="nopan"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-300">
                    Timeout (seconds)
                  </Label>
                  <Input
                    type="number"
                    value={data.timeout ?? 30}
                    onChange={(e) => {
                      updateNodeField("timeout", parseInt(e.target.value) || 30);
                    }}
                    min={1}
                    max={300}
                    readOnly={!data.editable}
                    className="nopan"
                  />
                </div>
              </>
            )}
          </div>
          <WorkflowBlockParameterSelect
            workflowBlockId={id}
            workflowBlockType="http"
            parameters={data.parameterKeys}
            onParametersChange={(parameters) => {
              updateNodeField("parameterKeys", parameters);
            }}
            disabled={!data.editable}
          />
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="opacity-0"
        style={{ width: "50%", height: "50%" }}
      />
    </>
  );
}

export { HTTPNode };