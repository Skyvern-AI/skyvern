import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { CodeBlockParametersItem } from "./CodeBlockParametersItem.js";
import { OutputParameter } from "./OutputParameter.js";
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
