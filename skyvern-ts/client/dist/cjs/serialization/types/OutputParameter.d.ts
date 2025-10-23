import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const OutputParameter: core.serialization.ObjectSchema<serializers.OutputParameter.Raw, Skyvern.OutputParameter>;
export declare namespace OutputParameter {
    interface Raw {
        key: string;
        description?: string | null;
        output_parameter_id: string;
        workflow_id: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
