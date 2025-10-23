import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { TotpType } from "./TotpType.mjs";
export declare const NonEmptyPasswordCredential: core.serialization.ObjectSchema<serializers.NonEmptyPasswordCredential.Raw, Skyvern.NonEmptyPasswordCredential>;
export declare namespace NonEmptyPasswordCredential {
    interface Raw {
        password: string;
        username: string;
        totp?: string | null;
        totp_type?: TotpType.Raw | null;
    }
}
