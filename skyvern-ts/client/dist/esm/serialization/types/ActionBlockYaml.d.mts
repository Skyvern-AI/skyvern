import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { RunEngine } from "./RunEngine.mjs";
export declare const ActionBlockYaml: core.serialization.ObjectSchema<serializers.ActionBlockYaml.Raw, Skyvern.ActionBlockYaml>;
export declare namespace ActionBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        url?: string | null;
        title?: string | null;
        engine?: RunEngine.Raw | null;
        navigation_goal?: string | null;
        error_code_mapping?: Record<string, string | null | undefined> | null;
        max_retries?: number | null;
        parameter_keys?: string[] | null;
        complete_on_download?: boolean | null;
        download_suffix?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        cache_actions?: boolean | null;
        disable_cache?: boolean | null;
    }
}
