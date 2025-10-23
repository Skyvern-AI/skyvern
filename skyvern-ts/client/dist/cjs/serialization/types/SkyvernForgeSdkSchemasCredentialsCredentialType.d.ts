import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const SkyvernForgeSdkSchemasCredentialsCredentialType: core.serialization.Schema<serializers.SkyvernForgeSdkSchemasCredentialsCredentialType.Raw, Skyvern.SkyvernForgeSdkSchemasCredentialsCredentialType>;
export declare namespace SkyvernForgeSdkSchemasCredentialsCredentialType {
    type Raw = "password" | "credit_card";
}
