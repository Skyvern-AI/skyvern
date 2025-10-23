import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const SelectOption: core.serialization.ObjectSchema<serializers.SelectOption.Raw, Skyvern.SelectOption>;
export declare namespace SelectOption {
    interface Raw {
        label?: string | null;
        value?: string | null;
        index?: number | null;
    }
}
