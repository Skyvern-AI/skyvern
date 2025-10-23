import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowRunBlockNavigationPayload: core.serialization.Schema<serializers.WorkflowRunBlockNavigationPayload.Raw, Skyvern.WorkflowRunBlockNavigationPayload>;
export declare namespace WorkflowRunBlockNavigationPayload {
    type Raw = Record<string, unknown> | unknown[] | string;
}
