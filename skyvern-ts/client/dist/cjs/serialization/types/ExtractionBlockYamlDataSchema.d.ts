import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ExtractionBlockYamlDataSchema: core.serialization.Schema<serializers.ExtractionBlockYamlDataSchema.Raw, Skyvern.ExtractionBlockYamlDataSchema>;
export declare namespace ExtractionBlockYamlDataSchema {
    type Raw = Record<string, unknown> | unknown[] | string;
}
