import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useUpdate } from "../../useUpdate";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { MicrosoftOAuthCredentialSelector } from "@/routes/workflows/components/MicrosoftOAuthCredentialSelector";
import { MICROSOFT_POWERBI_REQUIRED_SCOPES } from "@/util/microsoftScopes";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";

import { type AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import {
  POWERBI_QUERY_MAX_ROWS,
  type PowerBIQueryNode,
  type PowerBIQueryNodeData,
} from "./types";

function PowerBIQueryEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<PowerBIQueryNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "powerBIQuery") return null;

  return (
    <PowerBIQueryEditorBody
      key={blockId}
      blockId={blockId}
      data={nodeSlice.data}
    />
  );
}

function PowerBIQueryEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: PowerBIQueryNodeData;
}) {
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const update = useUpdate<PowerBIQueryNodeData>({
    id: blockId,
    editable: data.editable,
  });
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const canEdit = data.editable && !scopeReadOnly;
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const maxRowsInvalid =
    !Number.isInteger(data.maxRows) ||
    data.maxRows < 1 ||
    data.maxRows > POWERBI_QUERY_MAX_ROWS;

  return (
    <div data-testid="powerbi-query-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">
            Microsoft Power BI connection
          </Label>
          <HelpTooltip content="Choose an active Microsoft connection with Power BI dataset-read access." />
        </div>
        <MicrosoftOAuthCredentialSelector
          nodeId={blockId}
          value={data.connectionId}
          onChange={(connectionId) => update({ connectionId })}
          editable={canEdit}
          integrationName="Power BI"
          requiredScopes={MICROSOFT_POWERBI_REQUIRED_SCOPES}
        />
      </div>

      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">
            Dataset / model ID
          </Label>
          <HelpTooltip content="The Power BI dataset (semantic model) ID. Templates are resolved when the workflow runs; datasets are not discovered in the editor." />
        </div>
        <WorkflowBlockInputTextarea
          name="datasetId:powerbi"
          nodeId={blockId}
          value={data.datasetId}
          onChange={(datasetId) => update({ datasetId })}
          disabled={!canEdit}
          placeholder="Dataset ID or {{ dataset_id }}"
          className="nopan text-xs"
        />
      </div>

      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">DAX query</Label>
          <HelpTooltip content="A read-only DAX query. Templates are resolved and validated at runtime." />
        </div>
        <WorkflowBlockInputTextarea
          name="query:powerbi"
          nodeId={blockId}
          value={data.query}
          onChange={(query) => update({ query })}
          disabled={!canEdit}
          placeholder="EVALUATE 'Table'"
          className="nopan min-h-28 font-mono text-xs"
        />
      </div>

      <div className="space-y-2">
        <div className="flex gap-2">
          <Label
            htmlFor={`${blockId}-max-rows`}
            className="text-xs text-tertiary-foreground"
          >
            Maximum rows
          </Label>
          <HelpTooltip
            content={`Defaults to 1000. Use an integer from 1 to ${POWERBI_QUERY_MAX_ROWS}; reaching the cap fails the query rather than returning partial results.`}
          />
        </div>
        <Input
          id={`${blockId}-max-rows`}
          type="number"
          min={1}
          max={POWERBI_QUERY_MAX_ROWS}
          step={1}
          value={Number.isFinite(data.maxRows) ? data.maxRows : ""}
          onChange={(event) => {
            const maxRows = event.currentTarget.valueAsNumber;
            if (Number.isFinite(maxRows)) update({ maxRows });
          }}
          disabled={!canEdit}
          aria-invalid={maxRowsInvalid}
          aria-describedby={`${blockId}-max-rows-hint`}
          className="nopan text-xs"
        />
        <p
          id={`${blockId}-max-rows-hint`}
          className={`text-xs ${maxRowsInvalid ? "text-destructive" : "text-muted-foreground"}`}
        >
          {maxRowsInvalid
            ? `Enter an integer from 1 to ${POWERBI_QUERY_MAX_ROWS}.`
            : "The default is 1000 rows. The result must fit within this cap."}
        </p>
      </div>

      <Separator />

      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">
          Parameter bindings
        </Label>
        <ParametersMultiSelect
          availableOutputParameters={availableOutputParameterKeys}
          parameters={data.parameterKeys}
          onParametersChange={(parameterKeys) => update({ parameterKeys })}
          disabled={!canEdit}
        />
      </div>
    </div>
  );
}

export { PowerBIQueryEditor };
