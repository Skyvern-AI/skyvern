import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowRunBlockNavigationPayload: core.serialization.Schema<serializers.WorkflowRunBlockNavigationPayload.Raw, Skyvern.WorkflowRunBlockNavigationPayload>;
export declare namespace WorkflowRunBlockNavigationPayload {
    type Raw = Record<string, unknown> | unknown[] | string;
}
