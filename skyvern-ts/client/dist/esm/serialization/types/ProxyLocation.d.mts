import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ProxyLocation: core.serialization.Schema<serializers.ProxyLocation.Raw, Skyvern.ProxyLocation>;
export declare namespace ProxyLocation {
    type Raw = "RESIDENTIAL" | "US-CA" | "US-NY" | "US-TX" | "US-FL" | "US-WA" | "RESIDENTIAL_ES" | "RESIDENTIAL_IE" | "RESIDENTIAL_GB" | "RESIDENTIAL_IN" | "RESIDENTIAL_JP" | "RESIDENTIAL_FR" | "RESIDENTIAL_DE" | "RESIDENTIAL_NZ" | "RESIDENTIAL_ZA" | "RESIDENTIAL_AR" | "RESIDENTIAL_AU" | "RESIDENTIAL_BR" | "RESIDENTIAL_TR" | "RESIDENTIAL_CA" | "RESIDENTIAL_MX" | "RESIDENTIAL_IT" | "RESIDENTIAL_NL" | "RESIDENTIAL_ISP" | "NONE";
}
