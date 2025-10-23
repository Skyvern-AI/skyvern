import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { WorkflowDefinitionBlocksItem } from "./WorkflowDefinitionBlocksItem.js";
import { WorkflowDefinitionParametersItem } from "./WorkflowDefinitionParametersItem.js";
export declare const WorkflowDefinition: core.serialization.ObjectSchema<serializers.WorkflowDefinition.Raw, Skyvern.WorkflowDefinition>;
export declare namespace WorkflowDefinition {
    interface Raw {
        parameters: WorkflowDefinitionParametersItem.Raw[];
        blocks: WorkflowDefinitionBlocksItem.Raw[];
    }
}
