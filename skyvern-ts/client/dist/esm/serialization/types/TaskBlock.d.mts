import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
import { RunEngine } from "./RunEngine.mjs";
import { TaskBlockDataSchema } from "./TaskBlockDataSchema.mjs";
import { TaskBlockParametersItem } from "./TaskBlockParametersItem.mjs";
export declare const TaskBlock: core.serialization.ObjectSchema<serializers.TaskBlock.Raw, Skyvern.TaskBlock>;
export declare namespace TaskBlock {
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
        data_schema?: TaskBlockDataSchema.Raw | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_retries?: number | null;
        max_steps_per_run?: number | null;
        parameters?: TaskBlockParametersItem.Raw[] | null;
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
