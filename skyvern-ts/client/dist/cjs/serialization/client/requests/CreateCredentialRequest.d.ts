import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
import { CreateCredentialRequestCredential } from "../../types/CreateCredentialRequestCredential.js";
import { SkyvernForgeSdkSchemasCredentialsCredentialType } from "../../types/SkyvernForgeSdkSchemasCredentialsCredentialType.js";
export declare const CreateCredentialRequest: core.serialization.Schema<serializers.CreateCredentialRequest.Raw, Skyvern.CreateCredentialRequest>;
export declare namespace CreateCredentialRequest {
    interface Raw {
        name: string;
        credential_type: SkyvernForgeSdkSchemasCredentialsCredentialType.Raw;
        credential: CreateCredentialRequestCredential.Raw;
    }
}
