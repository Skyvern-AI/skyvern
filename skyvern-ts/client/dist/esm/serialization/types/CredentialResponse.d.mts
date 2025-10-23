import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { CredentialResponseCredential } from "./CredentialResponseCredential.mjs";
import { CredentialTypeOutput } from "./CredentialTypeOutput.mjs";
export declare const CredentialResponse: core.serialization.ObjectSchema<serializers.CredentialResponse.Raw, Skyvern.CredentialResponse>;
export declare namespace CredentialResponse {
    interface Raw {
        credential_id: string;
        credential: CredentialResponseCredential.Raw;
        credential_type: CredentialTypeOutput.Raw;
        name: string;
    }
}
