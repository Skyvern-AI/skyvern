import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { CredentialResponse } from "../types/CredentialResponse.mjs";
export declare const Response: core.serialization.Schema<serializers.getCredentials.Response.Raw, Skyvern.CredentialResponse[]>;
export declare namespace Response {
    type Raw = CredentialResponse.Raw[];
}
