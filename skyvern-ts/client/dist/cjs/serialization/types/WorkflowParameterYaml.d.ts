import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { WorkflowParameterType } from "./WorkflowParameterType.js";
import { WorkflowParameterYamlDefaultValue } from "./WorkflowParameterYamlDefaultValue.js";
export declare const WorkflowParameterYaml: core.serialization.ObjectSchema<serializers.WorkflowParameterYaml.Raw, Skyvern.WorkflowParameterYaml>;
export declare namespace WorkflowParameterYaml {
    interface Raw {
        key: string;
        description?: string | null;
        workflow_parameter_type: WorkflowParameterType.Raw;
        default_value?: WorkflowParameterYamlDefaultValue.Raw | null;
    }
}
