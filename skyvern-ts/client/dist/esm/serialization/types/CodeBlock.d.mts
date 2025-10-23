import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { CodeBlockParametersItem } from "./CodeBlockParametersItem.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const CodeBlock: core.serialization.ObjectSchema<serializers.CodeBlock.Raw, Skyvern.CodeBlock>;
export declare namespace CodeBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        code: string;
        parameters?: CodeBlockParametersItem.Raw[] | null;
    }
}
