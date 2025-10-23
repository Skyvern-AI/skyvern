import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const TotpType: core.serialization.Schema<serializers.TotpType.Raw, Skyvern.TotpType>;
export declare namespace TotpType {
    type Raw = "authenticator" | "email" | "text" | "none";
}
