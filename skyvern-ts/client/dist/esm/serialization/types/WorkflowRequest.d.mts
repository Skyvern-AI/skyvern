import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { WorkflowCreateYamlRequest } from "./WorkflowCreateYamlRequest.mjs";
export declare const WorkflowRequest: core.serialization.ObjectSchema<serializers.WorkflowRequest.Raw, Skyvern.WorkflowRequest>;
export declare namespace WorkflowRequest {
    interface Raw {
        json_definition?: WorkflowCreateYamlRequest.Raw | null;
        yaml_definition?: string | null;
    }
}
