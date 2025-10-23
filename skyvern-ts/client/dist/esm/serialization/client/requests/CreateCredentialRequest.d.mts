import type * as Skyvern from "../../../api/index.mjs";
import * as core from "../../../core/index.mjs";
import type * as serializers from "../../index.mjs";
import { CreateCredentialRequestCredential } from "../../types/CreateCredentialRequestCredential.mjs";
import { SkyvernForgeSdkSchemasCredentialsCredentialType } from "../../types/SkyvernForgeSdkSchemasCredentialsCredentialType.mjs";
export declare const CreateCredentialRequest: core.serialization.Schema<serializers.CreateCredentialRequest.Raw, Skyvern.CreateCredentialRequest>;
export declare namespace CreateCredentialRequest {
    interface Raw {
        name: string;
        credential_type: SkyvernForgeSdkSchemasCredentialsCredentialType.Raw;
        credential: CreateCredentialRequestCredential.Raw;
    }
}
