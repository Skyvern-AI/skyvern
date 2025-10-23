import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ActionStatus: core.serialization.Schema<serializers.ActionStatus.Raw, Skyvern.ActionStatus>;
export declare namespace ActionStatus {
    type Raw = "pending" | "skipped" | "failed" | "completed";
}
