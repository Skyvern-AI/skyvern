import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const RunEngine: core.serialization.Schema<serializers.RunEngine.Raw, Skyvern.RunEngine>;
export declare namespace RunEngine {
    type Raw = "skyvern-1.0" | "skyvern-2.0" | "openai-cua" | "anthropic-cua" | "ui-tars";
}
