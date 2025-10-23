import type * as Skyvern from "../index.mjs";
export interface WorkflowDefinition {
    parameters: Skyvern.WorkflowDefinitionParametersItem[];
    blocks: Skyvern.WorkflowDefinitionBlocksItem[];
}
