import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { TotpType } from "./TotpType.js";
export declare const NonEmptyPasswordCredential: core.serialization.ObjectSchema<serializers.NonEmptyPasswordCredential.Raw, Skyvern.NonEmptyPasswordCredential>;
export declare namespace NonEmptyPasswordCredential {
    interface Raw {
        password: string;
        username: string;
        totp?: string | null;
        totp_type?: TotpType.Raw | null;
    }
}
