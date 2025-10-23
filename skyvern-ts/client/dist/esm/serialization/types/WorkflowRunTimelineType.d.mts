import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowRunTimelineType: core.serialization.Schema<serializers.WorkflowRunTimelineType.Raw, Skyvern.WorkflowRunTimelineType>;
export declare namespace WorkflowRunTimelineType {
    type Raw = "thought" | "block";
}
