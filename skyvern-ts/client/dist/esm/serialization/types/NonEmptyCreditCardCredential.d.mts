import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const NonEmptyCreditCardCredential: core.serialization.ObjectSchema<serializers.NonEmptyCreditCardCredential.Raw, Skyvern.NonEmptyCreditCardCredential>;
export declare namespace NonEmptyCreditCardCredential {
    interface Raw {
        card_number: string;
        card_cvv: string;
        card_exp_month: string;
        card_exp_year: string;
        card_brand: string;
        card_holder_name: string;
    }
}
