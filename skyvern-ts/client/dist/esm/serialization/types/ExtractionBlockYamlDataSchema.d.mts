import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ExtractionBlockYamlDataSchema: core.serialization.Schema<serializers.ExtractionBlockYamlDataSchema.Raw, Skyvern.ExtractionBlockYamlDataSchema>;
export declare namespace ExtractionBlockYamlDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
