import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const UserDefinedError: core.serialization.ObjectSchema<serializers.UserDefinedError.Raw, Skyvern.UserDefinedError>;
export declare namespace UserDefinedError {
    interface Raw {
        error_code: string;
        reasoning: string;
        confidence_float: number;
    }
}
