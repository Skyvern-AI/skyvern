import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
export declare const Response: core.serialization.Schema<serializers.getRunTimeline.Response.Raw, Skyvern.WorkflowRunTimeline[]>;
export declare namespace Response {
    type Raw = serializers.WorkflowRunTimeline.Raw[];
}
