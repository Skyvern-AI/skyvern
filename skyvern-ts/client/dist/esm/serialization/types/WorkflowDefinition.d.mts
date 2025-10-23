import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { WorkflowDefinitionBlocksItem } from "./WorkflowDefinitionBlocksItem.mjs";
import { WorkflowDefinitionParametersItem } from "./WorkflowDefinitionParametersItem.mjs";
export declare const WorkflowDefinition: core.serialization.ObjectSchema<serializers.WorkflowDefinition.Raw, Skyvern.WorkflowDefinition>;
export declare namespace WorkflowDefinition {
    interface Raw {
        parameters: WorkflowDefinitionParametersItem.Raw[];
        blocks: WorkflowDefinitionBlocksItem.Raw[];
    }
}
