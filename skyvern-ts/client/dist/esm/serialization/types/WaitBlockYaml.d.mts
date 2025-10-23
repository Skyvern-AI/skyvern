import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const WaitBlockYaml: core.serialization.ObjectSchema<serializers.WaitBlockYaml.Raw, Skyvern.WaitBlockYaml>;
export declare namespace WaitBlockYaml {
    interface Raw {
        label: string;
        continue_on_failure?: boolean | null;
        model?: Record<string, unknown> | null;
        wait_sec?: number | null;
    }
}
