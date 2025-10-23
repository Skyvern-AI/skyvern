import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const UrlBlockYaml: core.serialization.ObjectSchema<serializers.UrlBlockYaml.Raw, Skyvern.UrlBlockYaml>;
export declare namespace UrlBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url: string;
    }
}
