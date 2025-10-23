import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const RunStatus: core.serialization.Schema<serializers.RunStatus.Raw, Skyvern.RunStatus>;
export declare namespace RunStatus {
    type Raw = "created" | "queued" | "running" | "timed_out" | "failed" | "terminated" | "completed" | "canceled";
}
