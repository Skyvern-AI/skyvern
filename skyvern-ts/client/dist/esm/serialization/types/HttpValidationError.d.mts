import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { ValidationError } from "./ValidationError.mjs";
export declare const HttpValidationError: core.serialization.ObjectSchema<serializers.HttpValidationError.Raw, Skyvern.HttpValidationError>;
export declare namespace HttpValidationError {
    interface Raw {
        detail?: ValidationError.Raw[] | null;
    }
}
