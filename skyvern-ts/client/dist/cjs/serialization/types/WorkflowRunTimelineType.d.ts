import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowRunTimelineType: core.serialization.Schema<serializers.WorkflowRunTimelineType.Raw, Skyvern.WorkflowRunTimelineType>;
export declare namespace WorkflowRunTimelineType {
    type Raw = "thought" | "block";
}
