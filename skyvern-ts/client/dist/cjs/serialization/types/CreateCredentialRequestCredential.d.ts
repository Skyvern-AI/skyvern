import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { NonEmptyCreditCardCredential } from "./NonEmptyCreditCardCredential.js";
import { NonEmptyPasswordCredential } from "./NonEmptyPasswordCredential.js";
export declare const CreateCredentialRequestCredential: core.serialization.Schema<serializers.CreateCredentialRequestCredential.Raw, Skyvern.CreateCredentialRequestCredential>;
export declare namespace CreateCredentialRequestCredential {
    type Raw = NonEmptyPasswordCredential.Raw | NonEmptyCreditCardCredential.Raw;
}
