import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ExtractionBlockDataSchema: core.serialization.Schema<serializers.ExtractionBlockDataSchema.Raw, Skyvern.ExtractionBlockDataSchema>;
export declare namespace ExtractionBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
