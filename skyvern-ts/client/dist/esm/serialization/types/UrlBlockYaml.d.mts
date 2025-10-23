import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const UrlBlockYaml: core.serialization.ObjectSchema<serializers.UrlBlockYaml.Raw, Skyvern.UrlBlockYaml>;
export declare namespace UrlBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url: string;
    }
}
