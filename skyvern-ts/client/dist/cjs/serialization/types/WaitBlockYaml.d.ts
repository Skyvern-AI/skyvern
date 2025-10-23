import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const WaitBlockYaml: core.serialization.ObjectSchema<serializers.WaitBlockYaml.Raw, Skyvern.WaitBlockYaml>;
export declare namespace WaitBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        wait_sec?: number | null;
    }
}
