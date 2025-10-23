import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WorkflowStatus: core.serialization.Schema<serializers.WorkflowStatus.Raw, Skyvern.WorkflowStatus>;
export declare namespace WorkflowStatus {
    type Raw = "published" | "draft" | "auto_generated";
}
