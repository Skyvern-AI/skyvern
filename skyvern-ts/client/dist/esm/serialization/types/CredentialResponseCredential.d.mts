import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { CreditCardCredentialResponse } from "./CreditCardCredentialResponse.mjs";
import { PasswordCredentialResponse } from "./PasswordCredentialResponse.mjs";
export declare const CredentialResponseCredential: core.serialization.Schema<serializers.CredentialResponseCredential.Raw, Skyvern.CredentialResponseCredential>;
export declare namespace CredentialResponseCredential {
    type Raw = PasswordCredentialResponse.Raw | CreditCardCredentialResponse.Raw;
}
