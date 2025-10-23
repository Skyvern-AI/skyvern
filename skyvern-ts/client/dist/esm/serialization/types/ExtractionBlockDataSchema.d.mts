import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ExtractionBlockDataSchema: core.serialization.Schema<serializers.ExtractionBlockDataSchema.Raw, Skyvern.ExtractionBlockDataSchema>;
export declare namespace ExtractionBlockDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
