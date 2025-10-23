import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
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
