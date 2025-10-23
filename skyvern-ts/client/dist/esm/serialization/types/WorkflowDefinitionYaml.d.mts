import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { WorkflowDefinitionYamlBlocksItem } from "./WorkflowDefinitionYamlBlocksItem.mjs";
import { WorkflowDefinitionYamlParametersItem } from "./WorkflowDefinitionYamlParametersItem.mjs";
export declare const WorkflowDefinitionYaml: core.serialization.ObjectSchema<serializers.WorkflowDefinitionYaml.Raw, Skyvern.WorkflowDefinitionYaml>;
export declare namespace WorkflowDefinitionYaml {
    interface Raw {
        parameters: WorkflowDefinitionYamlParametersItem.Raw[];
        blocks: WorkflowDefinitionYamlBlocksItem.Raw[];
    }
}
