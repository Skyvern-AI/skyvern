import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TaskV2BlockYaml: core.serialization.ObjectSchema<serializers.TaskV2BlockYaml.Raw, Skyvern.TaskV2BlockYaml>;
export declare namespace TaskV2BlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        prompt: string;
        url?: string | null;
        totp_verification_url?: string | null;
        totp_identifier?: string | null;
        max_iterations?: number | null;
        max_steps?: number | null;
        disable_cache?: boolean | null;
    }
}
