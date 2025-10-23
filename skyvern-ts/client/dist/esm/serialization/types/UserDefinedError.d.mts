import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const UserDefinedError: core.serialization.ObjectSchema<serializers.UserDefinedError.Raw, Skyvern.UserDefinedError>;
export declare namespace UserDefinedError {
    interface Raw {
        error_code: string;
        reasoning: string;
        confidence_float: number;
    }
}
