import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const SkyvernSchemasRunBlocksCredentialType: core.serialization.Schema<serializers.SkyvernSchemasRunBlocksCredentialType.Raw, Skyvern.SkyvernSchemasRunBlocksCredentialType>;
export declare namespace SkyvernSchemasRunBlocksCredentialType {
    type Raw = "skyvern" | "bitwarden" | "1password" | "azure_vault";
}
