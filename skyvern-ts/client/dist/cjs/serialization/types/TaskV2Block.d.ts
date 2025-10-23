import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { OutputParameter } from "./OutputParameter.js";
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
