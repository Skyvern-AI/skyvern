import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { OutputParameter } from "./OutputParameter.js";
import { WaitBlockParametersItem } from "./WaitBlockParametersItem.js";
export declare const WaitBlock: core.serialization.ObjectSchema<serializers.WaitBlock.Raw, Skyvern.WaitBlock>;
export declare namespace WaitBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        wait_sec: number;
        parameters?: WaitBlockParametersItem.Raw[] | null;
    }
}
