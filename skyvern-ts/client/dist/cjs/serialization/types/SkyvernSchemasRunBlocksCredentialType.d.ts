import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const SkyvernSchemasRunBlocksCredentialType: core.serialization.Schema<serializers.SkyvernSchemasRunBlocksCredentialType.Raw, Skyvern.SkyvernSchemasRunBlocksCredentialType>;
export declare namespace SkyvernSchemasRunBlocksCredentialType {
    type Raw = "skyvern" | "bitwarden" | "1password" | "azure_vault";
}
