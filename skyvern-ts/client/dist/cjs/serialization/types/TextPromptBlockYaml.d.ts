import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TextPromptBlockYaml: core.serialization.ObjectSchema<serializers.TextPromptBlockYaml.Raw, Skyvern.TextPromptBlockYaml>;
export declare namespace TextPromptBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        llm_key?: string | null;
        prompt: string;
        parameter_keys?: string[] | null;
        json_schema?: Record<string, unknown> | null;
    }
}
