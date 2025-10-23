import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { CredentialResponse } from "../types/CredentialResponse.js";
export declare const Response: core.serialization.Schema<serializers.getCredentials.Response.Raw, Skyvern.CredentialResponse[]>;
export declare namespace Response {
    type Raw = CredentialResponse.Raw[];
}
