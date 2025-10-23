import type * as Skyvern from "../index.js";
export interface WorkflowDefinition {
    parameters: Skyvern.WorkflowDefinitionParametersItem[];
    blocks: Skyvern.WorkflowDefinitionBlocksItem[];
}
