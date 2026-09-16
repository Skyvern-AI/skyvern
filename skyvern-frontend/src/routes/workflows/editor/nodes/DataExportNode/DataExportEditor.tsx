import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import { type AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { useUpdate } from "../../useUpdate";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { type DataExportNode, type DataExportNodeData } from "./types";

const dataSchemaExample = {
  type: "array",
  items: {
    type: "object",
    properties: { value: { type: "string" } },
  },
};

function DataExportEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<DataExportNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "dataExport") {
    return null;
  }
  return <DataExportEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function DataExportEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: DataExportNodeData;
}) {
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const update = useUpdate<DataExportNodeData>({
    id: blockId,
    editable: data.editable,
  });
  const availableOutputParameters = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  return (
    <div data-testid="data-export-block-form" className="space-y-4 p-4">
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">Records</Label>
        <Textarea
          className="nopan min-h-24 font-mono text-xs"
          disabled={!data.editable}
          onChange={(event) => update({ data: event.target.value })}
          placeholder="{{ extraction_output.extracted_information }}"
          value={data.data}
        />
      </div>
      <WorkflowDataSchemaInputGroup
        exampleValue={dataSchemaExample}
        onChange={(dataSchema) => update({ dataSchema })}
        suggestionContext={{ current_schema: data.dataSchema }}
        value={data.dataSchema}
      />
      <div className="space-y-2">
        <Label className="text-xs text-tertiary-foreground">
          Filename (optional)
        </Label>
        <Input
          className="nopan text-xs"
          disabled={!data.editable}
          onChange={(event) => update({ fileName: event.target.value })}
          placeholder="records"
          value={data.fileName}
        />
      </div>
      <ParametersMultiSelect
        availableOutputParameters={availableOutputParameters}
        onParametersChange={(parameterKeys) => update({ parameterKeys })}
        parameters={data.parameterKeys}
      />
    </div>
  );
}

export { DataExportEditor };
