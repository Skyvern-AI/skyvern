import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { CreditCardCredentialResponse } from "./CreditCardCredentialResponse.js";
import { PasswordCredentialResponse } from "./PasswordCredentialResponse.js";
export declare const CredentialResponseCredential: core.serialization.Schema<serializers.CredentialResponseCredential.Raw, Skyvern.CredentialResponseCredential>;
export declare namespace CredentialResponseCredential {
    type Raw = PasswordCredentialResponse.Raw | CreditCardCredentialResponse.Raw;
}
