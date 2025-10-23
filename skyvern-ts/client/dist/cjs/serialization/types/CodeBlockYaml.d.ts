import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const CodeBlockYaml: core.serialization.ObjectSchema<serializers.CodeBlockYaml.Raw, Skyvern.CodeBlockYaml>;
export declare namespace CodeBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        code: string;
        parameter_keys?: string[] | null;
    }
}
