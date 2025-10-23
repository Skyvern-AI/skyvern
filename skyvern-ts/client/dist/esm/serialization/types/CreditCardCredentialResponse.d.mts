import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const CreditCardCredentialResponse: core.serialization.ObjectSchema<serializers.CreditCardCredentialResponse.Raw, Skyvern.CreditCardCredentialResponse>;
export declare namespace CreditCardCredentialResponse {
    interface Raw {
        last_four: string;
        brand: string;
    }
}
