import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const SkyvernForgeSdkSchemasCredentialsCredentialType: core.serialization.Schema<serializers.SkyvernForgeSdkSchemasCredentialsCredentialType.Raw, Skyvern.SkyvernForgeSdkSchemasCredentialsCredentialType>;
export declare namespace SkyvernForgeSdkSchemasCredentialsCredentialType {
    type Raw = "password" | "credit_card";
}
