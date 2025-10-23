import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WorkflowStatus: core.serialization.Schema<serializers.WorkflowStatus.Raw, Skyvern.WorkflowStatus>;
export declare namespace WorkflowStatus {
    type Raw = "published" | "draft" | "auto_generated";
}
