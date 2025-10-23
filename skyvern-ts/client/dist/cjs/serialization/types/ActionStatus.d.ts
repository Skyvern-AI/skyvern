import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ActionStatus: core.serialization.Schema<serializers.ActionStatus.Raw, Skyvern.ActionStatus>;
export declare namespace ActionStatus {
    type Raw = "pending" | "skipped" | "failed" | "completed";
}
