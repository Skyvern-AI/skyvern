import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const OtpType: core.serialization.Schema<serializers.OtpType.Raw, Skyvern.OtpType>;
export declare namespace OtpType {
    type Raw = "totp" | "magic_link";
}
