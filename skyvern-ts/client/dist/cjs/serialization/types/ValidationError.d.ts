import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ValidationErrorLocItem } from "./ValidationErrorLocItem.js";
export declare const ValidationError: core.serialization.ObjectSchema<serializers.ValidationError.Raw, Skyvern.ValidationError>;
export declare namespace ValidationError {
    interface Raw {
        loc: ValidationErrorLocItem.Raw[];
        msg: string;
        type: string;
    }
}
