import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { RunEngine } from "./RunEngine.js";
export declare const FileDownloadBlockYaml: core.serialization.ObjectSchema<serializers.FileDownloadBlockYaml.Raw, Skyvern.FileDownloadBlockYaml>;
export declare namespace FileDownloadBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        navigation_goal: string;
        url?: string | null;
        title?: string | null;
        engine?: RunEngine.Raw | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_retries?: number | null;
        max_steps_per_run?: number | null;
        parameter_keys?: string[] | null;
        download_suffix?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        cache_actions?: boolean | null;
        disable_cache?: boolean | null;
        download_timeout?: number | null;
    }
}
