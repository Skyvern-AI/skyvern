import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const RunStatus: core.serialization.Schema<serializers.RunStatus.Raw, Skyvern.RunStatus>;
export declare namespace RunStatus {
    type Raw = "created" | "queued" | "running" | "timed_out" | "failed" | "terminated" | "completed" | "canceled";
}
