import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const CreditCardCredentialResponse: core.serialization.ObjectSchema<serializers.CreditCardCredentialResponse.Raw, Skyvern.CreditCardCredentialResponse>;
export declare namespace CreditCardCredentialResponse {
    interface Raw {
        last_four: string;
        brand: string;
    }
}
