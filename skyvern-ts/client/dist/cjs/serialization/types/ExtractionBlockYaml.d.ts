import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ExtractionBlockYamlDataSchema } from "./ExtractionBlockYamlDataSchema.js";
import { RunEngine } from "./RunEngine.js";
export declare const ExtractionBlockYaml: core.serialization.ObjectSchema<serializers.ExtractionBlockYaml.Raw, Skyvern.ExtractionBlockYaml>;
export declare namespace ExtractionBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        data_extraction_goal: string;
        url?: string | null;
        title?: string | null;
        engine?: RunEngine.Raw | null;
        data_schema?: ExtractionBlockYamlDataSchema.Raw | null;
        max_retries?: number | null;
        max_steps_per_run?: number | null;
        parameter_keys?: string[] | null;
        cache_actions?: boolean | null;
        disable_cache?: boolean | null;
    }
}
