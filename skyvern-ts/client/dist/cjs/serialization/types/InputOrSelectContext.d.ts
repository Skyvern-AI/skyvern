import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
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
