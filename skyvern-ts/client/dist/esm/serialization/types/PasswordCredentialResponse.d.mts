import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { TotpType } from "./TotpType.mjs";
export declare const PasswordCredentialResponse: core.serialization.ObjectSchema<serializers.PasswordCredentialResponse.Raw, Skyvern.PasswordCredentialResponse>;
export declare namespace PasswordCredentialResponse {
    interface Raw {
        username: string;
        totp_type?: TotpType.Raw | null;
    }
}
