import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
import { WaitBlockParametersItem } from "./WaitBlockParametersItem.mjs";
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
