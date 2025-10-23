import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const HttpRequestBlockYaml: core.serialization.ObjectSchema<serializers.HttpRequestBlockYaml.Raw, Skyvern.HttpRequestBlockYaml>;
export declare namespace HttpRequestBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        method?: string | null;
        url?: string | null;
        headers?: Record<string, string | null | undefined> | null;
        body?: Record<string, unknown> | null;
        timeout?: number | null;
        follow_redirects?: boolean | null;
        parameter_keys?: string[] | null;
    }
}
