import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
export declare const TaskV2Block: core.serialization.ObjectSchema<serializers.TaskV2Block.Raw, Skyvern.TaskV2Block>;
export declare namespace TaskV2Block {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        prompt: string;
        url?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        max_iterations?: number | null;
        max_steps?: number | null;
    }
}
