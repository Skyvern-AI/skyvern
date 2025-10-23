import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { WorkflowCreateYamlRequest } from "./WorkflowCreateYamlRequest.js";
export declare const WorkflowRequest: core.serialization.ObjectSchema<serializers.WorkflowRequest.Raw, Skyvern.WorkflowRequest>;
export declare namespace WorkflowRequest {
    interface Raw {
        json_definition?: WorkflowCreateYamlRequest.Raw | null;
        yaml_definition?: string | null;
    }
}
