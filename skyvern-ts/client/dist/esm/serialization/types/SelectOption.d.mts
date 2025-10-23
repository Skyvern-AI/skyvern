import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const SelectOption: core.serialization.ObjectSchema<serializers.SelectOption.Raw, Skyvern.SelectOption>;
export declare namespace SelectOption {
    interface Raw {
        label?: string | null;
        value?: string | null;
        index?: number | null;
    }
}
