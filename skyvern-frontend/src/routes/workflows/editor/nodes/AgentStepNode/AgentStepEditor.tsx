import { useQuery } from "@tanstack/react-query";
import { useNodesData } from "@xyflow/react";

import { getClient } from "@/api/AxiosClient";
import { ReadableActionTypes } from "@/api/types";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import type { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";

import { helpTooltips } from "../../helpContent";
import { useUpdate } from "../../useUpdate";
import { workflowBlockTitle } from "../types";
import {
  AGENT_STEP_MAX_TURNS_LIMIT,
  agentStepDefaultDataSchema,
  agentStepToolsFromSelection,
  type AgentStepNode,
  type AgentStepNodeData,
} from "./types";

type AgentStepTool = {
  name: string;
  description: string;
  block_type: WorkflowBlockType | null;
};

const nonBlockToolTitles: Record<string, string> = {
  page_goto: workflowBlockTitle.goto_url,
  page_click: ReadableActionTypes.click,
  page_fill: ReadableActionTypes.input_text,
  page_extract: ReadableActionTypes.extract,
  read_run_data: "Read Run Data",
};

function agentStepToolTitle(tool: AgentStepTool): string {
  return (
    (tool.block_type && workflowBlockTitle[tool.block_type]) ||
    nonBlockToolTitles[tool.name] ||
    tool.name
  );
}

function useAgentStepToolsQuery() {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<AgentStepTool>>({
    queryKey: ["agent-step-tools"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<AgentStepTool>>("/agent_step_tools")
        .then((response) => response.data);
    },
    staleTime: 60 * 60 * 1000,
  });
}

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
  const { data: tools } = useAgentStepToolsQuery();
  const choosableTools = (tools ?? []).map((tool) => tool.name);
  const toolOptions = tools
    ? tools
        .map((tool) => ({
          label: agentStepToolTitle(tool),
          value: tool.name,
          description: tool.description,
        }))
        .sort((a, b) => a.label.localeCompare(b.label))
    : (data.tools ?? []).map((tool) => ({ label: tool, value: tool }));

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
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label
            className="text-xs text-tertiary-foreground"
            htmlFor={`${blockId}-tools`}
          >
            Tools
          </Label>
          <HelpTooltip content={helpTooltips["agentStep"]["tools"]} />
        </div>
        <MultiSelect
          allSelectedLabel="All tools"
          className="nopan"
          disabled={!data.editable || !tools}
          id={`${blockId}-tools`}
          maxCount={50}
          onValueChange={(tools) =>
            update({
              tools: agentStepToolsFromSelection(tools, choosableTools),
            })
          }
          options={toolOptions}
          placeholder={
            data.tools === null
              ? "All tools"
              : "Choose the tools this step may call"
          }
          value={data.tools ?? choosableTools}
        />
      </div>
    </div>
  );
}

export { AgentStepEditor };
