import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
