import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import { helpTooltips } from "../../helpContent";
import { useUpdate } from "../../useUpdate";
import {
  AGENT_STEP_MAX_TURNS_LIMIT,
  agentStepDefaultDataSchema,
  type AgentStepNode,
  type AgentStepNodeData,
} from "./types";

function AgentStepEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<AgentStepNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "agentStep") {
    return null;
  }
  return <AgentStepEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function AgentStepEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: AgentStepNodeData;
}) {
  const update = useUpdate<AgentStepNodeData>({
    id: blockId,
    editable: data.editable,
  });

  return (
    <div data-testid="agent-step-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">Prompt</Label>
          <HelpTooltip content={helpTooltips["agentStep"]["prompt"]} />
        </div>
        <WorkflowBlockInputTextarea
          name="prompt"
          nodeId={blockId}
          onChange={(value) => update({ prompt: value })}
          value={data.prompt}
          placeholder="What should the agent find out or do?"
          className="nopan text-xs"
        />
      </div>
      <WorkflowDataSchemaInputGroup
        deferKey={JSON.stringify([blockId, "dataSchema"])}
        exampleValue={agentStepDefaultDataSchema}
        helpTooltip={helpTooltips["agentStep"]["dataSchema"]}
        hideEnableToggle
        onChange={(dataSchema) => update({ dataSchema })}
        suggestionContext={{ current_schema: data.dataSchema }}
        value={data.dataSchema}
      />
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-tertiary-foreground">Max Turns</Label>
          <HelpTooltip content={helpTooltips["agentStep"]["maxTurns"]} />
        </div>
        <Input
          className="nopan w-52 text-xs"
          disabled={!data.editable}
          max={AGENT_STEP_MAX_TURNS_LIMIT}
          min={1}
          onChange={(event) => {
            const value = Number.parseInt(event.target.value, 10);
            if (Number.isNaN(value)) {
              return;
            }
            update({
              maxTurns: Math.min(
                AGENT_STEP_MAX_TURNS_LIMIT,
                Math.max(1, value),
              ),
            });
          }}
          type="number"
          value={data.maxTurns}
        />
      </div>
    </div>
  );
}

export { AgentStepEditor };
