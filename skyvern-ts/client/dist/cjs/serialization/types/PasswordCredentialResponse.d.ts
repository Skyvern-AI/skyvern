import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { TotpType } from "./TotpType.js";
export declare const PasswordCredentialResponse: core.serialization.ObjectSchema<serializers.PasswordCredentialResponse.Raw, Skyvern.PasswordCredentialResponse>;
export declare namespace PasswordCredentialResponse {
    interface Raw {
        username: string;
        totp_type?: TotpType.Raw | null;
    }
}
