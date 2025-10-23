import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const OtpType: core.serialization.Schema<serializers.OtpType.Raw, Skyvern.OtpType>;
export declare namespace OtpType {
    type Raw = "totp" | "magic_link";
}
