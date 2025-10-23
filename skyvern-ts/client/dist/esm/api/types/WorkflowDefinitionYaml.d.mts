import type * as Skyvern from "../index.mjs";
export interface WorkflowDefinitionYaml {
    parameters: Skyvern.WorkflowDefinitionYamlParametersItem[];
    blocks: Skyvern.WorkflowDefinitionYamlBlocksItem[];
}
