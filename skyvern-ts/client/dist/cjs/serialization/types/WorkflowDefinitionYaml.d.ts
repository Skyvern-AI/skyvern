import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { WorkflowDefinitionYamlBlocksItem } from "./WorkflowDefinitionYamlBlocksItem.js";
import { WorkflowDefinitionYamlParametersItem } from "./WorkflowDefinitionYamlParametersItem.js";
export declare const WorkflowDefinitionYaml: core.serialization.ObjectSchema<serializers.WorkflowDefinitionYaml.Raw, Skyvern.WorkflowDefinitionYaml>;
export declare namespace WorkflowDefinitionYaml {
    interface Raw {
        parameters: WorkflowDefinitionYamlParametersItem.Raw[];
        blocks: WorkflowDefinitionYamlBlocksItem.Raw[];
    }
}
