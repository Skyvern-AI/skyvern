import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
export declare const Response: core.serialization.Schema<serializers.getRunTimeline.Response.Raw, Skyvern.WorkflowRunTimeline[]>;
export declare namespace Response {
    type Raw = serializers.WorkflowRunTimeline.Raw[];
}
