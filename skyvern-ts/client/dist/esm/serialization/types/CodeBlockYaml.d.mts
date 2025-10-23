import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
