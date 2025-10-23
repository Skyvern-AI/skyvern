import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { WorkflowParameterType } from "./WorkflowParameterType.mjs";
import { WorkflowParameterYamlDefaultValue } from "./WorkflowParameterYamlDefaultValue.mjs";
export declare const WorkflowParameterYaml: core.serialization.ObjectSchema<serializers.WorkflowParameterYaml.Raw, Skyvern.WorkflowParameterYaml>;
export declare namespace WorkflowParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        workflow_parameter_type: WorkflowParameterType.Raw;
        default_value?: WorkflowParameterYamlDefaultValue.Raw | null;
    }
}
