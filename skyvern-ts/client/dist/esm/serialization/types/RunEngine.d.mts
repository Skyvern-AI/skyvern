import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const RunEngine: core.serialization.Schema<serializers.RunEngine.Raw, Skyvern.RunEngine>;
export declare namespace RunEngine {
    type Raw = "skyvern-1.0" | "skyvern-2.0" | "openai-cua" | "anthropic-cua" | "ui-tars";
}
