import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { ErrorCodeMappingEditor } from "@/routes/workflows/editor/ErrorCodeMappingEditor";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

import type { AppNode } from "..";
import { helpTooltips } from "../../helpContent";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { dataSchemaExampleValue, errorMappingExampleValue } from "../types";
import type { WebSearchNode, WebSearchNodeData } from "./types";

function WebSearchEditor({ blockId }: { blockId: string }) {
  const node = useNodesData<WebSearchNode>(blockId);
  if (!node || node.type !== "web_search") {
    return null;
  }
  return <WebSearchEditorBody blockId={blockId} data={node.data} />;
}

function WebSearchEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: WebSearchNodeData;
}) {
  const update = useUpdate<WebSearchNodeData>({
    id: blockId,
    editable: data.editable,
  });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  return (
    <div data-testid="web-search-block-form" className="space-y-4">
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">Search Query</Label>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          name="query"
          value={data.query}
          onChange={(query) => update({ query })}
          placeholder="site:example.com search terms"
          className="nopan text-xs"
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-2">
          <Label className="text-xs text-tertiary-foreground">
            Search Provider
          </Label>
          <Select
            value={data.provider}
            disabled={!data.editable}
            onValueChange={(provider: WebSearchNodeData["provider"]) =>
              update({ provider })
            }
          >
            <SelectTrigger className="nopan text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">Automatic</SelectItem>
              <SelectItem value="google">Google</SelectItem>
              <SelectItem value="exa">Exa</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label
            htmlFor={`${blockId}-results`}
            className="text-xs text-tertiary-foreground"
          >
            Maximum Results
          </Label>
          <Input
            id={`${blockId}-results`}
            type="number"
            min={1}
            max={100}
            step={1}
            value={data.numResults}
            disabled={!data.editable}
            onChange={(event) => {
              const numResults = event.target.valueAsNumber;
              if (
                Number.isInteger(numResults) &&
                numResults >= 1 &&
                numResults <= 100
              ) {
                update({ numResults });
              }
            }}
            className="nopan text-xs"
          />
        </div>
      </div>
      <p className="text-xs text-tertiary-foreground">
        Automatic uses Google and switches to Exa if the initial search fails.
        Up to 100 results are returned when available.
      </p>
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">
          Prompt (optional)
        </Label>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          name="prompt"
          value={data.prompt}
          onChange={(prompt) => update({ prompt })}
          placeholder="What would you like to do with the search results?"
          className="nopan text-xs"
        />
        <p className="text-xs text-tertiary-foreground">
          Leave Prompt and Data Schema blank to return search results without AI
          processing. A prompt uses only the returned results and does not read
          linked pages.
        </p>
      </div>
      <ModelSelector
        className="nopan w-full text-xs"
        value={data.model}
        onChange={(model) => update({ model })}
      />
      <WorkflowDataSchemaInputGroup
        deferKey={JSON.stringify([blockId, "jsonSchema"])}
        exampleValue={dataSchemaExampleValue}
        value={data.jsonSchema}
        onChange={(jsonSchema) => update({ jsonSchema })}
        suggestionContext={{ current_schema: data.jsonSchema }}
      />
      <p className="text-xs text-tertiary-foreground">
        The answer is stored at {`{{ ${data.label}_output.prompt_output }}`}.
      </p>
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Label className="text-xs font-normal text-tertiary-foreground">
                Error Messages
              </Label>
              <HelpTooltip content={helpTooltips.webSearch.errorCodeMapping} />
            </div>
            <div className="w-52">
              <Switch
                checked={data.errorCodeMapping !== "null"}
                onCheckedChange={(checked) => {
                  if (!data.editable) return;
                  update({
                    errorCodeMapping: checked
                      ? JSON.stringify(errorMappingExampleValue, null, 2)
                      : "null",
                  });
                }}
              />
            </div>
          </div>
          {data.errorCodeMapping !== "null" && (
            <ErrorCodeMappingEditor
              deferKey={JSON.stringify([blockId, "errorCodeMapping"])}
              label={data.label}
              value={data.errorCodeMapping}
              onChange={(value) => update({ errorCodeMapping: value })}
              readOnly={!data.editable}
            />
          )}
        </div>
        <div className="flex-1 space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Continue on Failure
            </Label>
            <HelpTooltip content={helpTooltips.webSearch.continueOnFailure} />
          </div>
          <div className="flex items-center justify-end">
            <Switch
              checked={data.continueOnFailure}
              onCheckedChange={(checked) =>
                update({ continueOnFailure: checked })
              }
              disabled={!data.editable}
            />
          </div>
        </div>
      </div>
      <ParametersMultiSelect
        availableOutputParameters={outputParameterKeys}
        parameters={data.parameterKeys}
        onParametersChange={(parameterKeys) => update({ parameterKeys })}
      />
    </div>
  );
}

export { WebSearchEditor };
