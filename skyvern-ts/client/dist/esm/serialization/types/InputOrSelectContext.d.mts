import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const InputOrSelectContext: core.serialization.ObjectSchema<serializers.InputOrSelectContext.Raw, Skyvern.InputOrSelectContext>;
export declare namespace InputOrSelectContext {
    interface Raw {
        intention?: string | null;
        field?: string | null;
        is_required?: boolean | null;
        is_search_bar?: boolean | null;
        is_location_input?: boolean | null;
        is_date_related?: boolean | null;
    }
}
