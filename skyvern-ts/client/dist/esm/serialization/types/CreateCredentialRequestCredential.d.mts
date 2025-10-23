import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { NonEmptyCreditCardCredential } from "./NonEmptyCreditCardCredential.mjs";
import { NonEmptyPasswordCredential } from "./NonEmptyPasswordCredential.mjs";
export declare const CreateCredentialRequestCredential: core.serialization.Schema<serializers.CreateCredentialRequestCredential.Raw, Skyvern.CreateCredentialRequestCredential>;
export declare namespace CreateCredentialRequestCredential {
    type Raw = NonEmptyPasswordCredential.Raw | NonEmptyCreditCardCredential.Raw;
}
