import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { LoginBlockDataSchema } from "./LoginBlockDataSchema.js";
import { LoginBlockParametersItem } from "./LoginBlockParametersItem.js";
import { OutputParameter } from "./OutputParameter.js";
import { RunEngine } from "./RunEngine.js";
export declare const LoginBlock: core.serialization.ObjectSchema<serializers.LoginBlock.Raw, Skyvern.LoginBlock>;
export declare namespace LoginBlock {
    interface Raw {
        label: string;
        output_parameter: OutputParameter.Raw;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        disable_cache?: boolean | null;
        task_type?: string | null;
        url?: string | null;
        title?: string | null;
        engine?: RunEngine.Raw | null;
        complete_criterion?: string | null;
        terminate_criterion?: string | null;
        navigation_goal?: string | null;
        data_extraction_goal?: string | null;
        data_schema?: LoginBlockDataSchema.Raw | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_retries?: number | null;
        max_steps_per_run?: number | null;
        parameters?: LoginBlockParametersItem.Raw[] | null;
        complete_on_download?: boolean | null;
        download_suffix?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        cache_actions?: boolean | null;
        complete_verification?: boolean | null;
        include_action_history_in_verification?: boolean | null;
        download_timeout?: number | null;
    }
}
