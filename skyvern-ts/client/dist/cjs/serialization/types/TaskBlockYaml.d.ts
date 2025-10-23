import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { RunEngine } from "./RunEngine.js";
import { TaskBlockYamlDataSchema } from "./TaskBlockYamlDataSchema.js";
export declare const TaskBlockYaml: core.serialization.ObjectSchema<serializers.TaskBlockYaml.Raw, Skyvern.TaskBlockYaml>;
export declare namespace TaskBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url?: string | null;
        title?: string | null;
        engine?: RunEngine.Raw | null;
        navigation_goal?: string | null;
        data_extraction_goal?: string | null;
        data_schema?: TaskBlockYamlDataSchema.Raw | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_retries?: number | null;
        max_steps_per_run?: number | null;
        parameter_keys?: string[] | null;
        complete_on_download?: boolean | null;
        download_suffix?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        cache_actions?: boolean | null;
        disable_cache?: boolean | null;
        complete_criterion?: string | null;
        terminate_criterion?: string | null;
        complete_verification?: boolean | null;
        include_action_history_in_verification?: boolean | null;
    }
}
