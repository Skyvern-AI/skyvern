import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const TotpType: core.serialization.Schema<serializers.TotpType.Raw, Skyvern.TotpType>;
export declare namespace TotpType {
    type Raw = "authenticator" | "email" | "text" | "none";
}
