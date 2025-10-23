import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
import { TextPromptBlockParametersItem } from "./TextPromptBlockParametersItem.mjs";
export declare const TextPromptBlock: core.serialization.ObjectSchema<serializers.TextPromptBlock.Raw, Skyvern.TextPromptBlock>;
export declare namespace TextPromptBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        llm_key?: string | null;
        prompt: string;
        parameters?: TextPromptBlockParametersItem.Raw[] | null;
        json_schema?: Record<string, unknown> | null;
    }
}
