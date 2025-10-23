import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { HttpRequestBlockParametersItem } from "./HttpRequestBlockParametersItem.js";
import { OutputParameter } from "./OutputParameter.js";
export declare const HttpRequestBlock: core.serialization.ObjectSchema<serializers.HttpRequestBlock.Raw, Skyvern.HttpRequestBlock>;
export declare namespace HttpRequestBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        method?: string | null;
        url?: string | null;
        headers?: Record<string, string | null | undefined> | null;
        body?: Record<string, unknown> | null;
        timeout?: number | null;
        follow_redirects?: boolean | null;
        parameters?: HttpRequestBlockParametersItem.Raw[] | null;
    }
}
